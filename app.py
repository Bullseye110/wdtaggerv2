import argparse
import os
import time
from pathlib import Path

import gradio as gr
import huggingface_hub
import numpy as np
import onnxruntime as rt
import pandas as pd
from PIL import Image

TITLE = "WaifuDiffusion Tagger"
DESCRIPTION = """
Demo for the WaifuDiffusion tagger models

Example image by [ほし☆☆☆](https://www.pixiv.net/en/users/43565085)
"""

HF_TOKEN = os.environ.get("HF_TOKEN", "")

# Dataset v3 series of models:
SWINV2_MODEL_DSV3_REPO = "SmilingWolf/wd-swinv2-tagger-v3"
CONV_MODEL_DSV3_REPO = "SmilingWolf/wd-convnext-tagger-v3"
VIT_MODEL_DSV3_REPO = "SmilingWolf/wd-vit-tagger-v3"
VIT_LARGE_MODEL_DSV3_REPO = "SmilingWolf/wd-vit-large-tagger-v3"
EVA02_LARGE_MODEL_DSV3_REPO = "SmilingWolf/wd-eva02-large-tagger-v3"

# Dataset v2 series of models:
MOAT_MODEL_DSV2_REPO = "SmilingWolf/wd-v1-4-moat-tagger-v2"
SWIN_MODEL_DSV2_REPO = "SmilingWolf/wd-v1-4-swinv2-tagger-v2"
CONV_MODEL_DSV2_REPO = "SmilingWolf/wd-v1-4-convnext-tagger-v2"
CONV2_MODEL_DSV2_REPO = "SmilingWolf/wd-v1-4-convnextv2-tagger-v2"
VIT_MODEL_DSV2_REPO = "SmilingWolf/wd-v1-4-vit-tagger-v2"

# IdolSankaku series of models:
EVA02_LARGE_MODEL_IS_DSV1_REPO = "deepghs/idolsankaku-eva02-large-tagger-v1"
SWINV2_MODEL_IS_DSV1_REPO = "deepghs/idolsankaku-swinv2-tagger-v1"

# Files to download from the repos
MODEL_FILENAME = "model.onnx"
LABEL_FILENAME = "selected_tags.csv"

# https://github.com/toriato/stable-diffusion-webui-wd14-tagger/blob/a9eacb1eff904552d3012babfa28b57e1d3e295c/tagger/ui.py#L368
kaomojis = [
    "0_0",
    "(o)_(o)",
    "+_+",
    "+_-",
    "._.",
    "<o>_<o>",
    "<|>_<|>",
    "=_=",
    ">_<",
    "3_3",
    "6_9",
    ">_o",
    "@_@",
    "^_^",
    "o_o",
    "u_u",
    "x_x",
    "|_|",
    "||_||",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-slider-step", type=float, default=0.05)
    parser.add_argument("--score-general-threshold", type=float, default=0.35)
    parser.add_argument("--score-character-threshold", type=float, default=0.85)
    return parser.parse_args()


def load_labels(dataframe) -> list[str]:
    name_series = dataframe["name"]
    name_series = name_series.map(
        lambda x: x.replace("_", " ") if x not in kaomojis else x
    )
    tag_names = name_series.tolist()

    rating_indexes = list(np.where(dataframe["category"] == 9)[0])
    general_indexes = list(np.where(dataframe["category"] == 0)[0])
    character_indexes = list(np.where(dataframe["category"] == 4)[0])
    return tag_names, rating_indexes, general_indexes, character_indexes


def mcut_threshold(probs):
    """
    Maximum Cut Thresholding (MCut)
    Largeron, C., Moulin, C., & Gery, M. (2012). MCut: A Thresholding Strategy
     for Multi-label Classification. In 11th International Symposium, IDA 2012
     (pp. 172-183).
    """
    sorted_probs = probs[probs.argsort()[::-1]]
    difs = sorted_probs[:-1] - sorted_probs[1:]
    t = difs.argmax()
    thresh = (sorted_probs[t] + sorted_probs[t + 1]) / 2
    return thresh


class Predictor:
    def __init__(self):
        self.model_target_size = None
        self.last_loaded_repo = None

    def download_model(self, model_repo):
        csv_path = huggingface_hub.hf_hub_download(
            model_repo,
            LABEL_FILENAME,
            use_auth_token=HF_TOKEN,
        )
        model_path = huggingface_hub.hf_hub_download(
            model_repo,
            MODEL_FILENAME,
            use_auth_token=HF_TOKEN,
        )
        return csv_path, model_path

    def load_model(self, model_repo):
        if model_repo == self.last_loaded_repo:
            return

        csv_path, model_path = self.download_model(model_repo)

        tags_df = pd.read_csv(csv_path)
        sep_tags = load_labels(tags_df)

        self.tag_names = sep_tags[0]
        self.rating_indexes = sep_tags[1]
        self.general_indexes = sep_tags[2]
        self.character_indexes = sep_tags[3]

        model = rt.InferenceSession(model_path)
        _, height, width, _ = model.get_inputs()[0].shape
        self.model_target_size = height

        self.last_loaded_repo = model_repo
        self.model = model

    def prepare_image(self, image):
        target_size = self.model_target_size

        canvas = Image.new("RGBA", image.size, (255, 255, 255))
        canvas.alpha_composite(image)
        image = canvas.convert("RGB")

        # Pad image to square
        image_shape = image.size
        max_dim = max(image_shape)
        pad_left = (max_dim - image_shape[0]) // 2
        pad_top = (max_dim - image_shape[1]) // 2

        padded_image = Image.new("RGB", (max_dim, max_dim), (255, 255, 255))
        padded_image.paste(image, (pad_left, pad_top))

        # Resize
        if max_dim != target_size:
            padded_image = padded_image.resize(
                (target_size, target_size),
                Image.BICUBIC,
            )

        # Convert to numpy array
        image_array = np.asarray(padded_image, dtype=np.float32)

        # Convert PIL-native RGB to BGR
        image_array = image_array[:, :, ::-1]

        return np.expand_dims(image_array, axis=0)

    def predict(
        self,
        image,
        model_repo,
        general_thresh,
        general_mcut_enabled,
        character_thresh,
        character_mcut_enabled,
    ):
        self.load_model(model_repo)

        image = self.prepare_image(image)

        input_name = self.model.get_inputs()[0].name
        label_name = self.model.get_outputs()[0].name
        preds = self.model.run([label_name], {input_name: image})[0]

        labels = list(zip(self.tag_names, preds[0].astype(float)))

        # First 4 labels are actually ratings: pick one with argmax
        ratings_names = [labels[i] for i in self.rating_indexes]
        rating = dict(ratings_names)

        # Then we have general tags: pick any where prediction confidence > threshold
        general_names = [labels[i] for i in self.general_indexes]

        if general_mcut_enabled:
            general_probs = np.array([x[1] for x in general_names])
            general_thresh = mcut_threshold(general_probs)

        general_res = [x for x in general_names if x[1] > general_thresh]
        general_res = dict(general_res)

        # Everything else is characters: pick any where prediction confidence > threshold
        character_names = [labels[i] for i in self.character_indexes]

        if character_mcut_enabled:
            character_probs = np.array([x[1] for x in character_names])
            character_thresh = mcut_threshold(character_probs)
            character_thresh = max(0.15, character_thresh)

        character_res = [x for x in character_names if x[1] > character_thresh]
        character_res = dict(character_res)

        sorted_general_strings = sorted(
            general_res.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        sorted_general_strings = [x[0] for x in sorted_general_strings]
        sorted_general_strings = (
            ", ".join(sorted_general_strings).replace("(", "\\(").replace(")", "\\)")
        )

        return sorted_general_strings, rating, character_res, general_res


def save_caption_to_file(image_path, caption, output_dir):
    if not output_dir:
        return
    
    output_path = Path(output_dir) / f"{Path(image_path).stem}.txt"
    os.makedirs(output_dir, exist_ok=True)
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(caption)

def process_batch_images(
    images,
    model_repo,
    general_thresh,
    general_mcut_enabled,
    character_thresh,
    character_mcut_enabled,
    output_dir,
    progress=gr.Progress(track_tqdm=True)
):
    predictor = Predictor()
    results = []
    total = len(images)
    
    for idx, img_path in enumerate(images, 1):
        try:
            img = Image.open(img_path)
            if img.mode != "RGBA":
                img = img.convert("RGBA")
                
            sorted_general_strings, rating, _, _ = predictor.predict(
                img,
                model_repo,
                general_thresh,
                general_mcut_enabled,
                character_thresh,
                character_mcut_enabled,
            )
            
            # Save caption to file if output directory is specified
            if output_dir:
                save_caption_to_file(img_path, sorted_general_strings, output_dir)
            
            # Add just the caption to results
            results.append(sorted_general_strings)
            
            # Update progress
            progress_text = f"Processing {Path(img_path).name} ({idx}/{total})"
            
            # Yield both progress and results
            yield progress_text, "\n\n".join(results)
            
            # Small delay to prevent overwhelming the UI
            time.sleep(0.1)
            
        except Exception as e:
            error_msg = f"Error processing {Path(img_path).name}: {str(e)}"
            results.append(error_msg)
            yield f"Error ({idx}/{total})", "\n\n".join(results)
    
    return "Done!", "\n\n".join(results)

def main():
    args = parse_args()
    predictor = Predictor()
    
    dropdown_list = [
        SWINV2_MODEL_DSV3_REPO,
        CONV_MODEL_DSV3_REPO,
        VIT_MODEL_DSV3_REPO,
        VIT_LARGE_MODEL_DSV3_REPO,
        EVA02_LARGE_MODEL_DSV3_REPO,
        MOAT_MODEL_DSV2_REPO,
        SWIN_MODEL_DSV2_REPO,
        CONV_MODEL_DSV2_REPO,
        CONV2_MODEL_DSV2_REPO,
        VIT_MODEL_DSV2_REPO,
        SWINV2_MODEL_IS_DSV1_REPO,
        EVA02_LARGE_MODEL_IS_DSV1_REPO,
    ]

    with gr.Blocks(title=TITLE) as demo:
        with gr.Column():
            gr.Markdown(value=f"<h1 style='text-align: center; margin-bottom: 1rem'>{TITLE}</h1>")
            gr.Markdown(value=DESCRIPTION)
            
            with gr.Tabs():
                with gr.TabItem("Single Image"):
                    with gr.Row():
                        with gr.Column(variant="panel"):
                            image = gr.Image(type="pil", image_mode="RGBA", label="Input")
                            model_repo = gr.Dropdown(
                                dropdown_list,
                                value=SWINV2_MODEL_DSV3_REPO,
                                label="Model",
                            )
                            with gr.Row():
                                general_thresh = gr.Slider(
                                    0,
                                    1,
                                    step=args.score_slider_step,
                                    value=args.score_general_threshold,
                                    label="General Tags Threshold",
                                    scale=3,
                                )
                                general_mcut_enabled = gr.Checkbox(
                                    value=False,
                                    label="Use MCut threshold",
                                    scale=1,
                                )
                            with gr.Row():
                                character_thresh = gr.Slider(
                                    0,
                                    1,
                                    step=args.score_slider_step,
                                    value=args.score_character_threshold,
                                    label="Character Tags Threshold",
                                    scale=3,
                                )
                                character_mcut_enabled = gr.Checkbox(
                                    value=False,
                                    label="Use MCut threshold",
                                    scale=1,
                                )
                            with gr.Row():
                                clear = gr.ClearButton(
                                    components=[
                                        image,
                                        model_repo,
                                        general_thresh,
                                        general_mcut_enabled,
                                        character_thresh,
                                        character_mcut_enabled,
                                    ],
                                    variant="secondary",
                                    size="lg",
                                )
                                submit = gr.Button(value="Submit", variant="primary", size="lg")
                        with gr.Column(variant="panel"):
                            sorted_general_strings = gr.Textbox(label="Output (string)")
                            rating = gr.Label(label="Rating")
                            character_res = gr.Label(label="Output (characters)")
                            general_res = gr.Label(label="Output (tags)")
                            clear.add(
                                [
                                    sorted_general_strings,
                                    rating,
                                    character_res,
                                    general_res,
                                ]
                            )
                
                with gr.TabItem("Batch Processing"):
                    with gr.Column(variant="panel"):
                        batch_images = gr.File(
                            file_count="multiple",
                            label="Upload Images",
                            file_types=["image"]
                        )
                        output_dir = gr.Textbox(
                            label="Caption Files Output Directory (optional)",
                            placeholder="Leave empty to disable caption file generation"
                        )
                        model_repo_batch = gr.Dropdown(
                            dropdown_list,
                            value=SWINV2_MODEL_DSV3_REPO,
                            label="Model",
                        )
                        with gr.Row():
                            general_thresh_batch = gr.Slider(
                                0,
                                1,
                                step=args.score_slider_step,
                                value=args.score_general_threshold,
                                label="General Tags Threshold",
                                scale=3,
                            )
                            general_mcut_enabled_batch = gr.Checkbox(
                                value=False,
                                label="Use MCut threshold",
                                scale=1,
                            )
                        with gr.Row():
                            character_thresh_batch = gr.Slider(
                                0,
                                1,
                                step=args.score_slider_step,
                                value=args.score_character_threshold,
                                label="Character Tags Threshold",
                                scale=3,
                            )
                            character_mcut_enabled_batch = gr.Checkbox(
                                value=False,
                                label="Use MCut threshold",
                                scale=1,
                            )
                        batch_submit = gr.Button(value="Process Batch", variant="primary", size="lg")
                        with gr.Row():
                            with gr.Column(scale=1):
                                progress_output = gr.Textbox(
                                    label="Progress",
                                    lines=2,
                                    max_lines=2,
                                    interactive=False
                                )
                            with gr.Column(scale=3):
                                batch_output = gr.Textbox(
                                    label="Results (updates in real-time)",
                                    lines=15,
                                    max_lines=30,
                                    show_copy_button=True,
                                    autoscroll=True
                                )

            submit.click(
                predictor.predict,
                inputs=[
                    image,
                    model_repo,
                    general_thresh,
                    general_mcut_enabled,
                    character_thresh,
                    character_mcut_enabled,
                ],
                outputs=[sorted_general_strings, rating, character_res, general_res],
            )
            
            batch_submit.click(
                process_batch_images,
                inputs=[
                    batch_images,
                    model_repo_batch,
                    general_thresh_batch,
                    general_mcut_enabled_batch,
                    character_thresh_batch,
                    character_mcut_enabled_batch,
                    output_dir,
                ],
                outputs=[progress_output, batch_output],
            )

            gr.Examples(
                [["power.jpg", SWINV2_MODEL_DSV3_REPO, 0.35, False, 0.85, False]],
                inputs=[
                    image,
                    model_repo,
                    general_thresh,
                    general_mcut_enabled,
                    character_thresh,
                    character_mcut_enabled,
                ],
            )

    demo.queue(max_size=20)  # Increased queue size for better batch handling
    demo.launch(share=True)

if __name__ == "__main__":
    main()
