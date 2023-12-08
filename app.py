import os
import operator
import argparse
from typing import Iterator
import requests
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
import torch
from transformers import (
    AutoModelForCausalLM,
    LlamaTokenizer,
    default_data_collator,
    get_scheduler,
    AutoConfig,
)
from transformers.integrations import HfDeepSpeedConfig
from threading import Thread
from typing import Any, Iterator, Union, List
from app_llama import llama_wrapper
import math
import gradio as gr
from googleapiclient.discovery import build
import requests

def load_hf_tokenizer(model_name_or_path, fast_tokenizer=True):
    tokenizer = LlamaTokenizer.from_pretrained("./checkpoint",
                                            padding_side = 'left',
                                            fast_tokenizer=True, legacy=True)

    return tokenizer

def google_search(query):
    api_key = "AIzaSyB_uFeOOa8GWykvF6SLPbnox4D1LMfxyIk"
    service = build("customsearch", "v1", developerKey=api_key)
    cse_id = "c21943deeeb464fb6"
    query = query

    result = service.cse().list(q=query, cx=cse_id).execute()
    Snippets = ""
    for item in result.get("items", []):
        Snippets = Snippets + (item['snippet'])
    return " ###Google Search Result###: " + "evidence:" + Snippets

if __name__=='__main__':
    print(os.getcwd())
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tokenizer = load_hf_tokenizer("./checkpoint", fast_tokenizer=True)
    tokenizer.pad_token=tokenizer.eos_token
    model = llama_wrapper(model_class=AutoModelForCausalLM, model_name_or_path="./checkpoint", tokenizer=tokenizer, bf16=True)
    model.model.to(device=device)


    def render_html(text: list[tuple[str, str]]):
        '''
        For chatbot output
        '''
        target_string = text[-1][1]
        if "true" in target_string:
            lowest_index = target_string.find("true")
            up_index = lowest_index + len("true")
            text[-1][1] = f"{target_string[:lowest_index]}<span style='background-color: yellow;'>true</span>{target_string[up_index:]}"
        elif "false" in target_string:
            lowest_index = target_string.find("false")
            up_index = lowest_index + len("false")
            text[-1][1] = f"{target_string[:lowest_index]}<span style='background-color: yellow;'>false</span>{target_string[up_index:]}"

        return text

    def scrape(url):
        driver = webdriver.Chrome()
        driver.get(url=url)
        webpage_content = driver.find_element(by=By.TAG_NAME, value="body").text
        return webpage_content

    def load_file(files):
        filepath = files.name
        if os.path.exists(filepath):
            with open(file=filepath, mode="r") as file:
                text = file.read()
            return text
        else:
            raise FileNotFoundError("File not exists")

    def clear_and_save_textbox(message: str) -> tuple[str, str]:
        return "", message

    def display_input(
        message: str, history: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        history.append((message, ""))
        return history

    def delete_prev_fn(
        history: list[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], str]:
        try:
            message, _ = history.pop()
        except IndexError:
            message = ""
        return history, message or ""

    def check_input_token_length(
    message: str, chat_history: list[tuple[str, str]], system_prompt: str
    ) -> None:
        input_token_length = model.get_input_token_length(message=message, chat_history=chat_history, system_prompt=system_prompt, file=False)
        if input_token_length > 1024:
            raise gr.Error(
                f"The accumulated input is too long ({input_token_length} > {1024}). Clear your chat history and try again."
            )

    def check_file_input_token_length(
    message: str, system_prompt: str
    ) -> None:
        input_token_length = model.get_input_token_length(message=message, system_prompt=system_prompt, file=True)
        if input_token_length > 1024:
            raise gr.Error(
                f"The accumulated input is too long ({input_token_length} > {1024}). Clear your chat history and try again."
            )

    def generate(
            message: str,
            scrape_content: str,
            history_with_input: list[tuple[str, str]],
            system_prompt: str,
            max_new_tokens: int,
            temperature: float,
            top_p: float,
            top_k: int,
    ) -> Iterator[list[tuple[str, str]]]:
        if max_new_tokens > 10000:
            raise ValueError
        scrape_content = "evidence:" + scrape_content
        message += scrape_content
        history = history_with_input[:-1]
        generator = model.run(
            message,
            system_prompt=system_prompt,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            file=False
        )
        try:
            first_response = next(generator)
            if("###Google Search On###" in message):
                yield history + [(message, first_response)]
            elif("###Google Search Off###" in message):
                actual_message = message.split('###Google Search Off###')[0]
                yield history + [(actual_message, first_response)]
            else:
                first_response = next(generator)
                actual_message = message.split('evidence:')[0]
                yield history + [(actual_message, first_response)]
        except StopIteration:
            yield history + [(message, "")]
        for response in generator:
            yield history + [(message, response)]

    def file_generate(
            message: str,
            file_content: str,
            history_with_input: list[tuple[str, str]],
            system_prompt: str,
            max_new_tokens: int,
            temperature: float,
            top_p: float,
            top_k: int,
    ) -> Iterator[list[tuple[str, str]]]:
        if max_new_tokens > 10000:
            raise ValueError
        file_content = "evidence:" + file_content
        message += file_content
        history = history_with_input[:-1]
        generator = model.run(
            message,
            system_prompt=system_prompt,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            file=True
        )
        try:
            first_response = next(generator)
            actual_message = message.split('evidence:')[0]
            yield history + [(actual_message, first_response)]
        except StopIteration:
            yield history + [(message, "")]
        for response in generator:
            yield history + [(message, response)]


    def two_columns_list(tab_data, chatbot):
            result = []
            for i in range(int(len(tab_data) / 2) + 1):
                row = gr.Row()
                with row:
                    for j in range(2):
                        index = 2 * i + j
                        if index >= len(tab_data):
                            break
                        item = tab_data[index]
                        with gr.Group():
                            gr.HTML(
                                f'<p style="color: black; font-weight: bold;">{item["act"]}</p>'
                            )
                            prompt_text = gr.Button(
                                label="",
                                value=f"{item['summary']}",
                                size="sm",
                                elem_classes="text-left-aligned",
                            )
                            prompt_text.click(
                                fn=clear_and_save_textbox,
                                inputs=prompt_text,
                                outputs=saved_input,
                                api_name=False,
                                queue=True,
                            ).then(
                                fn=display_input,
                                inputs=[saved_input, chatbot],
                                outputs=chatbot,
                                api_name=False,
                                queue=True,
                            ).then(
                                fn=lambda : None,
                                inputs=[saved_input, chatbot, system_prompt],
                                api_name=False,
                                queue=False,
                            ).success(
                                fn=lambda : None,
                                inputs=[
                                    saved_input,
                                    chatbot,
                                    system_prompt,
                                    max_new_tokens,
                                    temperature,
                                    top_p,
                                    top_k,
                                ],
                                outputs=chatbot,
                                api_name=False,
                            )
                    result.append(row)
            return result

    CSS = """
        .contain { display: flex; flex-direction: column;}
        #component-0 #component-1 #component-2 #component-4 #component-5 { height:71vh !important; }
        #component-0 #component-1 #component-24 > div:nth-child(2) { height:80vh !important; overflow-y:auto }
        .text-left-aligned {text-align: left !important; font-size: 16px;}
        .md.svelte-r3x3aw.chatbot {background-color: yellow;}
    """


    prompts = {}
    with gr.Blocks(css=CSS) as demo:
        with gr.Tab("Text"):
            with gr.Row(equal_height=True):
                with gr.Column(scale=2):
                    gr.Markdown(" ")
                    with gr.Group():
                        chatbot = gr.Chatbot(label="Chatbot", elem_classes="chatbot")
                        with gr.Row():
                            textbox = gr.Textbox(
                                container=False,
                                show_label=False,
                                placeholder="Type a message...",
                                lines=5,
                                scale=12,
                            )
                            submit_button = gr.Button(
                                "Submit", variant="primary", scale=1, min_width=0
                            )
                    with gr.Row():
                        retry_button = gr.Button("🔄  Retry", variant="secondary")
                        undo_button = gr.Button("↩️ Undo", variant="secondary")
                        clear_button = gr.Button("🗑️  Clear", variant="secondary")

                    saved_input = gr.State()
                    scrape_content = gr.State()
                    with gr.Row():
                        advanced_checkbox = gr.Checkbox(
                            label="Advanced",
                            value="",
                            container=False,
                            elem_classes="min_check",
                        )
                        prompts_checkbox = gr.Checkbox(
                            label="Prompts",
                            value="",
                            container=False,
                            elem_classes="min_check",

                        )

                    with gr.Column(visible=True) as advanced_column:
                        system_prompt = gr.Textbox(
                            label="System prompt", value="", lines=6
                        )
                        max_new_tokens = gr.Slider(
                            label="Max new tokens",
                            minimum=1,
                            maximum=1024,
                            step=1,
                            value=512,
                        )
                        temperature = gr.Slider(
                            label="Temperature",
                            minimum=0.1,
                            maximum=4.0,
                            step=0.1,
                            value=1.0,
                        )
                        top_p = gr.Slider(
                            label="Top-p (nucleus sampling)",
                            minimum=0.05,
                            maximum=1.0,
                            step=0.05,
                            value=0.95,
                        )
                        top_k = gr.Slider(
                            label="Top-k",
                            minimum=1,
                            maximum=50,
                            step=1,
                            value=20,
                        )

            submit_button.click(
                    fn=clear_and_save_textbox,
                    inputs=textbox,
                    outputs=[textbox, saved_input],
                    api_name=False,
                    queue=False,
                ).then(
                    fn=google_search,
                    inputs=saved_input,
                    outputs=scrape_content,
                    api_name=False,
                    queue=False,
                ).then(
                    fn=display_input,
                    inputs=[saved_input, chatbot],
                    outputs=chatbot,
                    api_name=False,
                    queue=False,
                ).then(
                    fn=check_input_token_length,
                    inputs=[saved_input, chatbot, system_prompt],
                    api_name=False,
                    queue=False,
                ).success(
                    fn=generate,
                    inputs=[
                        saved_input,
                        scrape_content,
                        chatbot,
                        system_prompt,
                        max_new_tokens,
                        temperature,
                        top_p,
                        top_k,
                    ],
                    outputs=chatbot,
                    api_name=False,
                ).then(
                    fn=render_html,
                    inputs=chatbot,
                    outputs=chatbot,
                    api_name=False,
                    queue=False
                )

            retry_button.click(
                fn=delete_prev_fn,
                inputs=chatbot,
                outputs=[chatbot, saved_input],
                api_name=False,
                queue=False,
            ).then(
                fn=display_input,
                inputs=[saved_input, chatbot],
                outputs=chatbot,
                api_name=False,
                queue=False,
            ).then(
                fn=generate,
                inputs=[
                    saved_input,
                    scrape_content,
                    chatbot,
                    system_prompt,
                    max_new_tokens,
                    temperature,
                    top_p,
                    top_k,
                ],
                outputs=chatbot,
                api_name=False,
            ).then(
                fn=render_html,
                inputs=chatbot,
                outputs=chatbot,
                api_name=False
            )

            undo_button.click(
                fn=delete_prev_fn,
                inputs=chatbot,
                outputs=[chatbot, saved_input],
                api_name=False,
                queue=False,
            ).then(
                fn=lambda x: x,
                inputs=[saved_input],
                outputs=textbox,
                api_name=False,
                queue=False,
            )

            clear_button.click(
                fn=lambda: ([], ""),
                outputs=[chatbot, saved_input],
                queue=False,
                api_name=False,
            )

        with gr.Tab("File"):
            saved_input = gr.State()
            file_input = gr.State()
            with gr.Row():
                with gr.Column():
                    chatbot = gr.Chatbot(label="Chatbot", elem_classes="chatbot")
                    filebox = gr.File(label="Upload News File", scale=2)
                    temp = gr.Textbox()
                    claim_content = gr.Textbox(label="Claim Content", value="", lines=6)
                    submit_file = gr.Button("Detect News")
                    """
                    system_prompt = gr.Textbox(
                        label="System prompt", value="", lines=6
                    )
                    max_new_tokens = gr.Slider(
                        label="Max new tokens",
                        minimum=1,
                        maximum=2048,
                        step=1,
                        value=2048,
                    )
                    temperature = gr.Slider(
                        label="Temperature",
                        minimum=0.1,
                        maximum=4.0,
                        step=0.1,
                        value=1.0,
                    )
                    top_p = gr.Slider(
                        label="Top-p (nucleus sampling)",
                        minimum=0.05,
                        maximum=1.0,
                        step=0.05,
                        value=0.95,
                    )
                    top_k = gr.Slider(
                        label="Top-k",
                        minimum=1,
                        maximum=1000,
                        step=1,
                    )
    """

            submit_file.click(
                fn=clear_and_save_textbox,
                inputs=claim_content,
                outputs=[claim_content, saved_input],
                api_name=False,
                queue=False,
            ).then(
                fn=load_file,
                inputs=filebox,
                outputs=file_input,
                api_name=False
            ).then(
                fn = lambda x: x,
                inputs = file_input,
                outputs = temp,
                api_name=False,
                queue=False
            ).then(
                fn=display_input,
                inputs=[saved_input, chatbot],
                outputs=chatbot,
                api_name=False,
                queue=False,
            ).then(
                fn=check_input_token_length,
                inputs=[saved_input, chatbot, system_prompt],
                api_name=False,
                queue=False,
            ).success(
                fn=file_generate,
                inputs=[
                    saved_input,
                    file_input,
                    chatbot,
                    system_prompt,
                    max_new_tokens,
                    temperature,
                    top_p,
                    top_k,
                ],
                outputs=chatbot,
                api_name=False,
            ).then(
                fn=render_html,
                inputs=chatbot,
                outputs=chatbot,
                api_name=False,
                queue=False
            )

    demo.queue(max_size=20).launch(
        show_api=False,
        share=True,
        ssl_verify=False,
        max_threads=20,
    )