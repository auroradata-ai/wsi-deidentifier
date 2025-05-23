import argparse
import io
import json
import os
import typing

import cv2
from google import genai
from google.genai import types
from PIL import Image


class BoundingBox(typing.TypedDict):
    x: float
    y: float
    width: float
    height: float
    label: str


USE_VERTEXAI = True


def read_image_as_jpeg_bytes(file_path: str) -> bytes:
    """Reads an image file, converts it to JPEG, and returns the bytes."""
    try:
        img = Image.open(file_path)
        # Ensure image is in RGB format for JPEG saving
        if img.mode != "RGB":
            img = img.convert("RGB")

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG")
        return buffer.getvalue()
    except Exception as e:
        print(f"Error processing image {file_path}: {e}")
        raise


async def gemini_extract(
    file_path: str,
    project: str | None = None,
    location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
) -> list[BoundingBox]:
    effective_project = (
        project if project is not None else os.getenv("GOOGLE_CLOUD_PROJECT")
    )

    client = genai.Client(
        api_key=not USE_VERTEXAI and os.getenv("GEMINI_API_KEY") or None,
        vertexai=USE_VERTEXAI,
        project=USE_VERTEXAI and effective_project or None,
        location=USE_VERTEXAI and location or None,
    )

    image_bytes = read_image_as_jpeg_bytes(file_path)

    files = (
        []
        if USE_VERTEXAI
        else [
            # For non-Vertex, we still need to upload the original file path
            # because the API expects a file URI, not raw bytes.
            # However, Gemini might support bytes directly in the future.
            client.files.upload(file=file_path),
        ]
    )
    model = "gemini-2.5-flash-preview-04-17"
    # model = "gemini-2.0-flash"

    if USE_VERTEXAI:
        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    else:
        if not files:
            raise ValueError("File upload failed for non-Vertex AI usage.")
        image_part = types.Part.from_uri(
            file_uri=files[0].uri,
            mime_type=files[0].mime_type,  # Use the uploaded file's mime type
        )

    contents = [
        types.Content(
            role="user",
            parts=[
                image_part,
                types.Part.from_text(
                    text="""Detect text and tissue with no more than 20 items. Output a json list where each entry contains the 2D bounding box in "box_2d" and tissue/text in "label"."""
                ),
            ],
        ),
    ]
    generate_content_config = types.GenerateContentConfig(
        temperature=0,
        response_mime_type="text/plain",
        # thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

    print(f"Sending request to Gemini {model}...")

    result = await client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=generate_content_config,
    )

    print(f"Received response from Gemini {model}.")

    if not result.text:
        print("No response from Gemini.")
        return []

    if "```json" not in result.text:
        raise Exception("No JSON response from Gemini.")

    result_text = result.text.split("```json")[1].split("```")[0]
    parsed_response = json.loads(result_text)
    formatted_boxes: list[BoundingBox] = [
        {
            # box_2d format is [ymin, xmin, ymax, xmax]
            "x": box["box_2d"][1] / 1000,  # xmin is at index 1
            "y": box["box_2d"][0] / 1000,  # ymin is at index 0
            "width": (box["box_2d"][3] - box["box_2d"][1]) / 1000,  # xmax - xmin
            "height": (box["box_2d"][2] - box["box_2d"][0]) / 1000,  # ymax - ymin
            "label": box["label"],
        }
        for box in parsed_response
    ]
    return formatted_boxes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Detect tissue in an image using Vertex AI Gemini."
    )
    parser.add_argument("file_path", help="Path to the input image file.")
    parser.add_argument(
        "--project",
        help="Google Cloud project ID.",
        default=os.getenv("GOOGLE_CLOUD_PROJECT"),
    )
    parser.add_argument(
        "--location",
        help="Google Cloud location (e.g., us-central1).",
        default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
    )

    args = parser.parse_args()

    import asyncio

    boxes = asyncio.run(gemini_extract(args.file_path, args.project, args.location))
    print(boxes)
    if boxes:
        img = cv2.imread(args.file_path)
        if img is None:
            print(f"Error: Could not read image file {args.file_path}")
        else:
            height, width, _ = img.shape
            for box in boxes:
                x1 = int(box["x"] * width)
                y1 = int(box["y"] * height)
                x2 = int((box["x"] + box["width"]) * width)
                y2 = int((box["y"] + box["height"]) * height)
                label = box["label"]

                # Draw bounding box
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)

                # Put label text above the box
                label_y = y1 - 10 if y1 - 10 > 10 else y1 + 10
                cv2.putText(
                    img,
                    label,
                    (x1, label_y),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.9,
                    (0, 255, 0),
                    2,
                )

            output_dir = "annotated_tissue"
            os.makedirs(output_dir, exist_ok=True)
            base_filename = os.path.basename(args.file_path)
            output_filename = os.path.join(output_dir, f"annotated_{base_filename}")
            cv2.imwrite(output_filename, img)
            print(f"Annotated image saved to: {output_filename}")
    else:
        print("No bounding boxes found to draw.")
