import argparse
import os
import sys

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from transformers import AutoTokenizer, BitsAndBytesConfig, CLIPImageProcessor

from model.LISA import LISAForCausalLM
from model.llava import conversation as conversation_lib
from model.llava.mm_utils import tokenizer_image_token
from model.segment_anything.utils.transforms import ResizeLongestSide
from utils.utils import (DEFAULT_IM_END_TOKEN, DEFAULT_IM_START_TOKEN,
                         DEFAULT_IMAGE_TOKEN, IMAGE_TOKEN_INDEX)

# Fixed prompt
PROMPT1 = """
- People who are walking or riding kick scooters (including electric kick scooters), segways, skateboards, etc. are labeled as pedestrians.
- People inside other vehicles are not labeled, except for people standing on the top of cars/trucks or standing on flatbeds of trucks.
- A person riding a bicycle is not labeled as a pedestrian, but labeled as a cyclist instead.
- Mannequins, statues, billboards, posters, or reflections of people are not labeled.
- Include small child or carrying small items (smaller than 2m in size such as umbrella or small handbag or a sign).
- Include small mobility devices like a kick scooter (including electric kick scooter), a segway, a skateboard, etc
- If the pedestrian is carrying an object larger than 2m, or pushing a bike or shopping cart, the bounding box does not include the additional object.
- If the pedestrian is pushing a stroller with a child in it, separate bounding boxes are created for the pedestrian and the child. The stroller is not included in the child bounding box.
- If pedestrians overlap each other, they are labeled as separate objects. If they overlap then the bounding boxes can overlap as well.
"""

PROMPT2 = """
Output segmentation masks for pedestrians with:
1. Label visible pedestrians.
2. Do not label if pedestrian identity is unclear.
3. Label people walking or on scooters, segways, skateboards.
4. Do not label people inside vehicles, unless on top/flatbeds.
5. Cyclists are not labeled as pedestrians.
6. Ignore mannequins, posters, reflections, etc.
7. Use one box if pedestrian carries small items (<2m) or a child.
8. Label scooter/segway/skateboard riders with one box.
9. Exclude large items (>2m) or pushed carts/bikes from box.
10. Use separate boxes for pedestrian and child in stroller; exclude stroller.
11. Overlapping pedestrians get separate (possibly overlapping) boxes.
"""

PROMPT3 = """
pedestrian
"""

PROMPT4 = """
"""

Prompts = {
  1:PROMPT1,
  2:PROMPT2,
  3:PROMPT3,
  4:PROMPT4
}

def collect_pairs(root_dir):
    image_label_pairs = []

    for root, dirs, _ in os.walk(root_dir):
        if os.path.basename(root) == "images":
            for clip_id in dirs:
                image_clip_path = os.path.join(root, clip_id)
                label_clip_path = image_clip_path.replace("/images/", "/labels/")

                for timestamp in os.listdir(image_clip_path):
                    image_ts_path = os.path.join(image_clip_path, timestamp)
                    label_ts_path = os.path.join(label_clip_path, timestamp)

                    if not os.path.isdir(label_ts_path):
                        continue  # skip unmatched labels

                    for frame_file in os.listdir(image_ts_path):
                        image_path = os.path.join(image_ts_path, frame_file)
                        label_path = os.path.join(label_ts_path, frame_file.replace(".jpeg", ".png"))

                        if os.path.exists(label_path):
                            image_label_pairs.append((image_path, label_path))

    return image_label_pairs

def parse_args(args):
    parser = argparse.ArgumentParser(description="LISA chat")
    parser.add_argument("--version", default="xinlai/LISA-13B-llama2-v1")
    parser.add_argument("--vis_save_path", default="/content/LISA/gt_good_sample", type=str)
    parser.add_argument(
        "--precision",
        default="bf16",
        type=str,
        choices=["fp32", "bf16", "fp16"],
        help="precision for inference",
    )
    parser.add_argument("--image_size", default=1024, type=int, help="image size")
    parser.add_argument("--model_max_length", default=512, type=int)
    parser.add_argument("--lora_r", default=8, type=int)
    parser.add_argument(
        "--vision-tower", default="openai/clip-vit-large-patch14", type=str
    )
    parser.add_argument("--local-rank", default=0, type=int, help="node rank")
    parser.add_argument("--load_in_8bit", action="store_true", default=False)
    parser.add_argument("--load_in_4bit", action="store_true", default=False)
    parser.add_argument("--use_mm_start_end", action="store_true", default=True)
    parser.add_argument(
        "--conv_type",
        default="llava_v1",
        type=str,
        choices=["llava_v1", "llava_llama_2"],
    )
    parser.add_argument("--folder_path", default="/content/LISA/gt_good_sample")
    parser.add_argument("--prompt_number", type=int, default=1, help="Number of the prompt to use (e.g. 1, 2)")
    return parser.parse_args(args)


def preprocess(
    x,
    pixel_mean=torch.Tensor([123.675, 116.28, 103.53]).view(-1, 1, 1),
    pixel_std=torch.Tensor([58.395, 57.12, 57.375]).view(-1, 1, 1),
    img_size=1024,
) -> torch.Tensor:
    """Normalize pixel values and pad to a square input."""
    # Normalize colors
    x = (x - pixel_mean) / pixel_std
    # Pad
    h, w = x.shape[-2:]
    padh = img_size - h
    padw = img_size - w
    x = F.pad(x, (0, padw, 0, padh))
    return x


def main(args):
    args = parse_args(args)
    os.makedirs(args.vis_save_path, exist_ok=True)

    # Create model
    tokenizer = AutoTokenizer.from_pretrained(
        args.version,
        cache_dir=None,
        model_max_length=args.model_max_length,
        padding_side="right",
        use_fast=False,
    )
    tokenizer.pad_token = tokenizer.unk_token
    args.seg_token_idx = tokenizer("[SEG]", add_special_tokens=False).input_ids[0]


    torch_dtype = torch.float32
    if args.precision == "bf16":
        torch_dtype = torch.bfloat16
    elif args.precision == "fp16":
        torch_dtype = torch.half

    kwargs = {"torch_dtype": torch_dtype}
    if args.load_in_4bit:
        kwargs.update(
            {
                "torch_dtype": torch.half,
                "load_in_4bit": True,
                "quantization_config": BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_compute_dtype=torch.float16,
                    bnb_4bit_use_double_quant=True,
                    bnb_4bit_quant_type="nf4",
                    llm_int8_skip_modules=["visual_model"],
                ),
            }
        )
    elif args.load_in_8bit:
        kwargs.update(
            {
                "torch_dtype": torch.half,
                "quantization_config": BitsAndBytesConfig(
                    llm_int8_skip_modules=["visual_model"],
                    load_in_8bit=True,
                ),
            }
        )

    model = LISAForCausalLM.from_pretrained(
        args.version, low_cpu_mem_usage=True, vision_tower=args.vision_tower, seg_token_idx=args.seg_token_idx, **kwargs
    )

    model.config.eos_token_id = tokenizer.eos_token_id
    model.config.bos_token_id = tokenizer.bos_token_id
    model.config.pad_token_id = tokenizer.pad_token_id

    model.get_model().initialize_vision_modules(model.get_model().config)
    vision_tower = model.get_model().get_vision_tower()
    vision_tower.to(dtype=torch_dtype)

    if args.precision == "bf16":
        model = model.bfloat16().cuda()
    elif (
        args.precision == "fp16" and (not args.load_in_4bit) and (not args.load_in_8bit)
    ):
        vision_tower = model.get_model().get_vision_tower()
        model.model.vision_tower = None
        import deepspeed

        model_engine = deepspeed.init_inference(
            model=model,
            dtype=torch.half,
            replace_with_kernel_inject=True,
            replace_method="auto",
        )
        model = model_engine.module
        model.model.vision_tower = vision_tower.half().cuda()
    elif args.precision == "fp32":
        model = model.float().cuda()

    vision_tower = model.get_model().get_vision_tower()
    vision_tower.to(device=args.local_rank)

    clip_image_processor = CLIPImageProcessor.from_pretrained(model.config.vision_tower)
    transform = ResizeLongestSide(args.image_size)

    model.eval()
    pairs = collect_pairs(args.folder_path)
    print(f"Total pairs collected: {pairs}")
    print(f"Prompt: {Prompts[args.prompt_number]}")
    for img_path, lbl_path in pairs:
        torch.cuda.empty_cache()
        print(f"Processing image: {img_path} {lbl_path}")
        conv = conversation_lib.conv_templates[args.conv_type].copy()
        conv.messages = []

        #prompt = input("Please input your prompt: ")
        prompt = Prompts[args.prompt_number]

        prompt = DEFAULT_IMAGE_TOKEN + "\n" + prompt
        if args.use_mm_start_end:
            replace_token = (
                DEFAULT_IM_START_TOKEN + DEFAULT_IMAGE_TOKEN + DEFAULT_IM_END_TOKEN
            )
            prompt = prompt.replace(DEFAULT_IMAGE_TOKEN, replace_token)

        conv.append_message(conv.roles[0], prompt)
        conv.append_message(conv.roles[1], "")
        prompt = conv.get_prompt()

        # image_path = input("Please input the image path: ")
        image_path = img_path
        if not os.path.exists(image_path):
            print("File not found in {}".format(image_path))
            continue

        image_np = cv2.imread(image_path)
        image_np = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGB)
        original_size_list = [image_np.shape[:2]]

        image_clip = (
            clip_image_processor.preprocess(image_np, return_tensors="pt")[
                "pixel_values"
            ][0]
            .unsqueeze(0)
            .cuda()
        )
        if args.precision == "bf16":
            image_clip = image_clip.bfloat16()
        elif args.precision == "fp16":
            image_clip = image_clip.half()
        else:
            image_clip = image_clip.float()

        image = transform.apply_image(image_np)
        resize_list = [image.shape[:2]]

        image = (
            preprocess(torch.from_numpy(image).permute(2, 0, 1).contiguous())
            .unsqueeze(0)
            .cuda()
        )
        if args.precision == "bf16":
            image = image.bfloat16()
        elif args.precision == "fp16":
            image = image.half()
        else:
            image = image.float()

        input_ids = tokenizer_image_token(prompt, tokenizer, return_tensors="pt")
        input_ids = input_ids.unsqueeze(0).cuda()

        output_ids, pred_masks = model.evaluate(
            image_clip,
            image,
            input_ids,
            resize_list,
            original_size_list,
            max_new_tokens=512,
            tokenizer=tokenizer,
        )
        output_ids = output_ids[0][output_ids[0] != IMAGE_TOKEN_INDEX]

        text_output = tokenizer.decode(output_ids, skip_special_tokens=False)
        text_output = text_output.replace("\n", "").replace("  ", " ")
        print("text_output: ", text_output)
        # Extract parent path
        parent_dir = "/".join(lbl_path.split("/")[:-1])

        # Extract filename without extension
        base_name = lbl_path.split("/")[-1].split(".")[0]
        for i, pred_mask in enumerate(pred_masks):
            if pred_mask.shape[0] == 0:
                continue

            pred_mask = pred_mask.detach().cpu().numpy()[0]
            pred_mask = pred_mask > 0
            # Convert to uint8 for inspection and saving
            pred_mask1 = pred_mask.astype(np.uint8)

            # # Print min, max, and value counts
            # print("Min:", pred_mask.min())
            # print("Max:", pred_mask.max())
            # print("Unique values and counts:", np.unique(pred_mask, return_counts=True))
            # break
            # Convert to grayscale [0, 255]
            gray_mask = pred_mask1 * 255

            # Save as grayscale PNG
            save_path = "{}/{}_LISA_mask_{}_prompt{}.png".format(parent_dir, base_name, i, args.prompt_number)
            cv2.imwrite(save_path, gray_mask)
            # save_path = "{}/{}_mask_{}.jpg".format(
            #    args.vis_save_path, image_path.split("/")[-1].split(".")[0], i
            #)
            # save_path = "{}/{}_LISA_mask_{}.png".format(parent_dir, base_name, i)

            # cv2.imwrite(save_path, pred_mask * 100)
            print("{} has been saved.".format(save_path))

            # save_path = "{}/{}_masked_img_{}.jpg".format(
            #    args.vis_save_path, image_path.split("/")[-1].split(".")[0], i
            #)
            save_path = "{}/{}_LISA_masked_img_{}_prompt{}.png".format(parent_dir, base_name, i, args.prompt_number)

            save_img = image_np.copy()
            save_img[pred_mask] = (
                image_np * 0.5
                + pred_mask[:, :, None].astype(np.uint8) * np.array([255, 0, 0]) * 0.5
            )[pred_mask]
            save_img = cv2.cvtColor(save_img, cv2.COLOR_RGB2BGR)
            cv2.imwrite(save_path, save_img)
            print("{} has been saved.".format(save_path))


if __name__ == "__main__":
    main(sys.argv[1:])
