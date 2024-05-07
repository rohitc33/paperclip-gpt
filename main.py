from enum import Enum
from getpass import getpass
import json
import os
import re
import sys
import time

import dotenv
from openai import OpenAI
from selenium import webdriver
from selenium.webdriver.common.by import By


GAME_URL = "https://www.decisionproblem.com/paperclips/index2.html"
PLAN_MODEL = "gpt-4"
ACT_MODEL = "gpt-3.5-turbo"
REFLECT_MODEL = "gpt-4-turbo"
SYSTEM_PROMPT = """\
You are an AI in charge of a paperclip manufacturing facility owned by Champaign Stationary Co.
Your ultimate goal is to maximize the rate of production of paperclips.
"""
PLAN_INSTRUCTIONS = """\
The next section shows two snapshots of your factory and the market. The first snapshot was taken \
5 time steps ago, and the second snapshot is current. The snapshots include actions that are/were \
available for you to take, shown in the following format:

[index](label)

The effect of an action can usually be inferred by its label and contextual info from the snapshot. \
However, if you are unsure of what an action does, please find out by choosing it! Projects have \
indexes prefixed with "p". If the index is "x", the action is currently disabled due to \
insufficient resources (funds, trust, operations, etc.).

You are also shown a list of actions you have recently taken, along with their results.

Carefully consider all the actions available to you. Prioritize actions that may not \
immediately increase production or sales but are necessary for increasing production in the long \
run, as well as actions you have not chosen recently. For each action, write a sentence or two \
describing its effect and whether it would be helpful. Then, write a plan to follow for the next \
5 time steps, consisting of 5 actions (one for each time step). Each action can also be repeated \
up to 5 times during a time step. However, different actions cannot be executed in the same time \
step. You must choose a different action for each time step, unless there are fewer than 5 \
available actions.

Invest heavily in capital and projects which increase the rate of production, and do not worry \
excessively about unsold inventory. Consider that repeating actions more than once during a time \
step allows for faster progress. Execute more important actions first, since actions near the \
end of the plan are less likely to be executed due to earlier actions depleting resources. \
Prioritize actions without recent successful executions.
"""
ACT_INSTRUCTIONS = """\
Now, translate the plan you formulated above into machine-readable instructions. Each line in \
your response should correspond to a step in the plan and ONLY consist of two numbers: the \
index of an action followed by how many times to execute it (no more than 5). Here is an example:

5 4

This line would cause action 5 (chosen by index) to execute 4 times. Here is another example:

p3 1

This line would execute project 3. Since projects can only be executed once, the "1" is redundant.
"""
# Pay attention to any ALERTs in the snapshot. They indicate that the production of some resource has \
# stalled, and action is likely needed to resume it.
REFLECT_INSTRUCTIONS = """\
An action from your strategy was executed. You are shown snapshots from three times: immediately \
before the execution, immediately after the execution, and one time step later. Comparing these \
snapshots, write a sentence or two analyzing the effects of your action. State whether the \
expected result from the strategy above was achieved. Also state whether the execution of future \
actions in the strategy was hindered due to depletion of resources. Your response should be less \
than 50 words long.
"""
ACTION_DISABLED_RESULT = """\
This action was not executed because it was disabled when the execution was attempted.
"""


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


def create_message(role, content):
    return {
        "role": role,
        "content": content,
    }


class GameState:
    data = {}
    dynamic_ids = set()
    num_clips = 0
    num_actions = 0
    num_projects = 0


def generate_snapshot_section(element, actions, state, section=None):
    if element.tag_name == "b":
        section = section.replace(element.text, f"\n## {element.text}\n")
    elif element.tag_name == "button":
        if "\n" not in element.text:
            index = str(state.num_actions)
            state.num_actions += 1
        else:
            index = f"p{element.get_attribute("id")[len("projectButton"):]}"
            state.num_projects += 1

        if element.is_enabled() and not (index == "0" and state.num_clips > 0):
            section = section.replace(element.text, f"[{index}]({element.text})")
            actions[index] = element
        else:
            section = section.replace(element.text, f"[x]({element.text})")
    elif element.tag_name in ["div", "p"] and element.is_displayed():
        if not section:
            section = re.sub(r"(\n\s*)+\n+", "\n\n", element.text)
        children = element.find_elements(By.XPATH, "./*")
        for child in children:
            section = generate_snapshot_section(child, actions, state, section)
    return section


def generate_snapshot(driver, state):
    prompt = ""
    actions = {}

    state.num_actions = 0
    state.num_projects = 0

    console_text = driver.find_element(By.CSS_SELECTOR, ".console #readout1").text
    if not console_text.startswith("Welcome"):
        prompt += f"Message: {console_text}\n\n"

    num_clips = driver.find_element(By.CSS_SELECTOR, "#topDiv #clips").text
    prompt += f"Paperclips: {num_clips}\n"
    state.num_clips = int(num_clips.replace(",", ""))

    left_col = driver.find_element(By.ID, "leftColumn")
    prompt += generate_snapshot_section(left_col, actions, state) + "\n\n"
    middle_col = driver.find_element(By.ID, "middleColumn")
    prompt += generate_snapshot_section(middle_col, actions, state) + "\n\n"
    right_col = driver.find_element(By.ID, "rightColumn")
    prompt += generate_snapshot_section(right_col, actions, state)

    return prompt, actions


def parse_next_actions(actions, response):
    next_actions = []
    response_lines = response.split("\n")

    if len(response_lines) > 5:
        return None

    for line in response_lines:
        try:
            index, count = line.split()
            count = int(count)
            label = actions[index].text
            next_actions.append((index, label, count))
        except:
            return None

    return next_actions


def execute_action(actions, index, count):
    if index not in actions or count < 1 or count > 10:
        return False

    action = actions[index]

    for _ in range(count):
        if not (action.is_displayed() and action.is_enabled()):
            return True

        action.click()
        time.sleep(0.05)

    return True


def generate_response(client, model, messages):
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.6,
    )
    return response.choices[0].message.content.strip()


def run(api_key, save_dir):
    columns, _ = os.get_terminal_size(0)
    print(SYSTEM_PROMPT)

    client = OpenAI(api_key=api_key)
    system_message = create_message(Role.SYSTEM, SYSTEM_PROMPT)

    driver = webdriver.Chrome()
    driver.get(GAME_URL)

    step = 0
    action_success = False
    explored_actions = set()
    recent_actions = []
    next_actions = []
    messages = []
    snapshot_t0 = None
    snapshot_t1 = None
    plan_snapshot = None
    actions = None
    state = GameState()
    save_file = save_dir + "/save.json"

    if os.path.exists(save_file):
        with open(save_file, "r") as f:
            data = json.load(f)
            step = data["step"] + 1
            action_success = data["actionSuccess"]
            explored_actions = set(data["exploredActions"])
            recent_actions = data["recentActions"]
            next_actions = data["nextActions"]
            messages = data["messages"]
            snapshot_t0 = data["snapshot"]
            plan_snapshot = data.get("planSnapshot", None)
            local_storage = json.loads(data["localStorage"])

            for key, value in local_storage.items():
                driver.execute_script("localStorage.setItem(arguments[0], arguments[1])", key, value)
            driver.refresh()
    else:
        os.makedirs(save_dir)

    while True:
        if step == 0:
            _, actions = generate_snapshot(driver, state)

            for _ in range(500):
                actions["0"].click()
                time.sleep(0.01)

            snapshot_t1, actions = generate_snapshot(driver, state)
        elif next_actions is not None and (step - 1) % 5 < len(next_actions):
            time.sleep(15)

            snapshot_old = snapshot_t1
            snapshot_t1, actions = generate_snapshot(driver, state)

            messages = messages[:3]
            action_index, action_label, action_count = next_actions[(step - 1) % 5]

            if action_success:
                reflection_prompt_body = (
                    f"# ACTION (STEP {(step - 1) % 5 + 1} OF PLAN)\n\n"
                    f"[{action_index}]({action_label}) x{action_count}"
                    "\n\n# SNAPSHOT (IMMEDIATELY BEFORE)\n\n"
                    f"{snapshot_old}"
                    "\n\n# SNAPSHOT (IMMEDIATELY AFTER)\n\n"
                    f"{snapshot_t0}"
                    "\n\n# SNAPSHOT (1 TIME STEP AFTER)\n\n"
                    f"{snapshot_t1}"
                )

                print(columns * "=" + "\n")
                print(reflection_prompt_body)

                reflection_prompt = (
                    f"{REFLECT_INSTRUCTIONS}\n\n"
                    f"{reflection_prompt_body}"
                )
                messages.append(create_message(Role.USER, reflection_prompt))

                result = generate_response(client, REFLECT_MODEL, messages)

                if not action_index.startswith("p"):
                    explored_actions.add(action_index)
            else: # not execution_success
                result = ACTION_DISABLED_RESULT

            print(columns * "-" + "\n")
            print(f"Action: [{action_index}]({action_label}) x{action_count}")
            print(result + "\n")

            action_data = (
                step - 1,
                action_index,
                action_label,
                action_count,
                action_success,
                result,
            )
            recent_actions.insert(0, action_data)

            action_idxs = set()
            i = 0
            while i < len(recent_actions):
                old_step, old_index, _, _, success, _ = recent_actions[i]
                if step - old_step > 5 and (
                    old_index in action_idxs
                    or old_index.startswith("p")
                    or not success
                ):
                    del recent_actions[i]
                else:
                    if success:
                        action_idxs.add(old_index)
                    i += 1

        if step % 5 == 0:
            recent_action_idxs = set()
            recent_actions_str = ""
            for old_step, old_index, old_label, old_count, success, old_result in recent_actions:
                recent_actions_str += (
                    f"Step: t-{step - old_step}\n"
                    f"Action: [{old_index}]({old_label}) x{old_count}\n"
                    f"Result: {old_result}\n\n"
                )

                if step - old_step <= 5 and success:
                    recent_action_idxs.add(old_index)

                if step - old_step == 5:
                    recent_actions_str += "...\n\n"

            unexplored_actions = set(actions.keys()) - explored_actions
            unused_in_last_strategy = set(actions.keys()) - recent_action_idxs

            plan_prompt_body = (
                "# SNAPSHOT (5 TIME STEPS AGO)\n\n"
                f"{plan_snapshot or "n/a"}"
                "\n\n# SNAPSHOT (CURRENT)\n\n"
                f"{snapshot_t1}"
                "\n\n# RECENT ACTIONS\n\n"
                f"{recent_actions_str}"
                "\n\n# UNEXPLORED ACTIONS (TRY THESE)\n\n"
                f"{', '.join(unexplored_actions)}"
                # "\n\n# UNUSED ACTIONS IN LAST STRATEGY (TRY THESE)\n\n"
                # f"{', '.join(unused_in_last_strategy)}"
            )

            print(columns * "=" + "\n")
            print(plan_prompt_body)

            plan_prompt = (
                f"{PLAN_INSTRUCTIONS}\n\n"
                f"{plan_prompt_body}"
            )
            messages = [system_message, create_message(Role.USER, plan_prompt)]

            response = generate_response(client, PLAN_MODEL, messages)

            print(columns * "-" + "\n")
            print(response + "\n")
            messages.append(create_message(Role.ASSISTANT, response))
            messages.append(create_message(Role.USER, ACT_INSTRUCTIONS))

            response = generate_response(client, ACT_MODEL, messages)

            print(columns * "-" + "\n")
            print(response + "\n")
            next_actions = parse_next_actions(actions, response)

            if next_actions is None:
                continue

            plan_snapshot = snapshot_t1

        if step % 5 < len(next_actions):
            action_index, action_label, action_count = next_actions[step % 5]
            _, actions = generate_snapshot(driver, state)
            action_success = execute_action(actions, action_index, action_count)

        snapshot_t0, _ = generate_snapshot(driver, state)

        driver.save_screenshot(f"{save_dir}/{step}.png")

        with open(save_file, "w") as f:
            local_storage = driver.execute_script("save(); return JSON.stringify(localStorage);")
            data = {
                "step": step,
                "actionSuccess": action_success,
                "exploredActions": list(explored_actions),
                "recentActions": recent_actions,
                "nextActions": next_actions,
                "messages": messages,
                "snapshot": snapshot_t0,
                "planSnapshot": plan_snapshot,
                "localStorage": local_storage,
            }
            json.dump(data, f)

        step += 1


if __name__ == "__main__":
    api_key = os.getenv("OPENAI_API_KEY")

    if api_key is None:
        api_key = getpass('OpenAI API key:')
        try:
            dotenv_file = dotenv.find_dotenv()
            dotenv.set_key(dotenv_file, 'OPENAI_API_KEY', api_key)
        except:
            pass

    run(api_key, sys.argv[1])
