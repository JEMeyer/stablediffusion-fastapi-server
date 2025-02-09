import os
import time
import logging
import asyncio
from uuid import uuid4
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from PIL import Image
import torch
from torch import autocast
from diffusers import AutoPipelineForText2Image, AutoPipelineForImage2Image
from transformers.utils import move_cache
import base64
import sentry_sdk
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

# Migrate cache as suggested by the error message
move_cache()

sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN"), traces_sample_rate=1.0, profiles_sample_rate=1.0
)

logging.basicConfig(
    level=logging.INFO if os.environ.get("ENV") != "production" else logging.WARNING
)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(SentryAsgiMiddleware)


# ----------------------
#   ENV VARIABLES
# ----------------------
def get_env_bool(var_name: str, default: bool) -> bool:
    return os.environ.get(var_name, str(default)).lower() in ["1", "true", "yes"]


MODEL_NAME = os.environ.get("MODEL_NAME", "stabilityai/sdxl-turbo")
ENABLE_TXT2IMG = get_env_bool("ENABLE_TXT2IMG", True)
ENABLE_IMG2IMG = get_env_bool("ENABLE_IMG2IMG", True)
XFORMERS_ENABLED = get_env_bool("XFORMERS_ENABLED", False)

logger.info(f"Model Name: {MODEL_NAME}")
logger.info(f"Enable txt2img: {ENABLE_TXT2IMG}")
logger.info(f"Enable img2img: {ENABLE_IMG2IMG}")
logger.info(f"Xformers Enabled: {XFORMERS_ENABLED}")

# ----------------------
#   PREP GPU + MODEL
# ----------------------
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
logger.info(f"Using device: {device}")

if XFORMERS_ENABLED:
    try:
        import xformers  # noqa: F401

        logger.info(
            "xFormers is installed. Enabling memory-efficient attention if supported."
        )
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.benchmark = True  # Optional: Optimize CUDA performance
    except ImportError:
        logger.warning(
            "xformers not installed, cannot enable memory-efficient attention."
        )

# Create directory for uploaded images
IMAGE_DIR = "uploaded_images"
Path(IMAGE_DIR).mkdir(parents=True, exist_ok=True)

# Lock to ensure single-GPU concurrency is managed
gpu_lock = asyncio.Lock()

# Optional pipeline references
txt2img_pipeline = None
img2img_pipeline = None


@app.on_event("startup")
async def load_models():
    global txt2img_pipeline, img2img_pipeline
    try:
        if ENABLE_TXT2IMG or ENABLE_IMG2IMG:
            base_pipeline = AutoPipelineForText2Image.from_pretrained(
                MODEL_NAME, torch_dtype=torch.float16
            ).to(device)
            logger.info(f"Base pipeline for {MODEL_NAME} loaded.")

        if ENABLE_TXT2IMG:
            txt2img_pipeline = base_pipeline
            logger.info("txt2img pipeline enabled.")

        if ENABLE_IMG2IMG:
            img2img_pipeline = AutoPipelineForImage2Image.from_pretrained(
                MODEL_NAME,
                torch_dtype=torch.float16,
                vae=base_pipeline.vae,
                text_encoder=base_pipeline.text_encoder,
                tokenizer=base_pipeline.tokenizer,
                unet=base_pipeline.unet,
                scheduler=base_pipeline.scheduler,
                safety_checker=None,
                feature_extractor=None,
            ).to(device)
            logger.info("img2img pipeline enabled.")
    except Exception as e:
        logger.error(f"Failed to load pipelines: {e}", exc_info=True)
        raise RuntimeError(f"Failed to initialize model: {e}")


# ----------------------
#       DATA MODELS
# ----------------------
class Txt2ImgInput(BaseModel):
    prompt: str
    negative_prompt: str = ""
    num_inference_steps: int = 4
    guidance_scale: float = 7.5


class Img2ImgInput(BaseModel):
    prompt: str
    negative_prompt: str = ""
    file_id: str
    num_inference_steps: int = 4
    strength: float = 0.5
    guidance_scale: float = 7.5


class ImageGenerationRequest(BaseModel):
    prompt: str
    n: int = 1
    size: str = "512x512"
    response_format: str = "url"


# ----------------------
#     MIDDLEWARE
# ----------------------
@app.middleware("http")
async def log_duration(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    logger.info(f"Request to {request.url.path} took {duration:.2f} seconds")
    return response


# ----------------------
#       HELPERS
# ----------------------
async def stream_image(image: Image.Image):
    img_byte_arr = BytesIO()
    image.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)
    chunk_size = 1024  # 1KB chunks

    while True:
        data = img_byte_arr.read(chunk_size)
        if not data:
            break
        yield data


async def save_image_and_get_url(image: Image.Image, file_id: str) -> str:
    output_dir = Path("output_images")
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"{file_id}.png"
    image.save(file_path)
    return f"/output_images/{file_id}.png"


async def encode_image_to_base64(image: Image.Image) -> str:
    img_byte_arr = BytesIO()
    image.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)
    return base64.b64encode(img_byte_arr.getvalue()).decode("utf-8")


# ----------------------
#      ENDPOINTS
# ----------------------


@app.post("/txt2img")
async def txt2img(input_data: Txt2ImgInput):
    if not ENABLE_TXT2IMG or txt2img_pipeline is None:
        raise HTTPException(
            status_code=400, detail="txt2img is disabled on this server."
        )

    try:
        async with gpu_lock:
            with autocast(device.type):
                with torch.no_grad():
                    result = txt2img_pipeline(
                        prompt=input_data.prompt,
                        negative_prompt=input_data.negative_prompt,
                        num_inference_steps=input_data.num_inference_steps,
                        guidance_scale=input_data.guidance_scale,
                    )
                    image = result.images[0]

        return StreamingResponse(
            stream_image(image),
            media_type="image/png",
            headers={"Content-Disposition": f"attachment; filename={uuid4()}.png"},
        )

    except Exception as e:
        logger.error(f"Error in txt2img: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    try:
        file_uuid = str(uuid4())
        filepath = os.path.join(IMAGE_DIR, f"{file_uuid}.png")

        file_content = await file.read()
        with open(filepath, "wb") as f:
            f.write(file_content)

        return {"file_id": file_uuid}
    except Exception as e:
        logger.error(f"Failed to upload image: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/img2img")
async def img2img(input_data: Img2ImgInput):
    if not ENABLE_IMG2IMG or img2img_pipeline is None:
        raise HTTPException(
            status_code=400, detail="img2img is disabled on this server."
        )

    filepath = os.path.join(IMAGE_DIR, f"{input_data.file_id}.png")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Uploaded file not found on disk.")

    pil_image = Image.open(filepath).convert("RGB")

    try:
        async with gpu_lock:
            with autocast(device.type):
                with torch.no_grad():
                    result = img2img_pipeline(
                        prompt=input_data.prompt,
                        negative_prompt=input_data.negative_prompt,
                        image=pil_image,
                        num_inference_steps=input_data.num_inference_steps,
                        strength=input_data.strength,
                        guidance_scale=input_data.guidance_scale,
                    )
                    image = result.images[0]

        return StreamingResponse(
            stream_image(image),
            media_type="image/png",
            headers={"Content-Disposition": f"attachment; filename={uuid4()}.png"},
        )
    except Exception as e:
        logger.error(f"Error in img2img: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/images/generations")
async def generate_images(request: ImageGenerationRequest):
    if not ENABLE_TXT2IMG or txt2img_pipeline is None:
        raise HTTPException(
            status_code=503, detail="Text-to-Image generation is unavailable."
        )

    try:
        async with asyncio.Lock():
            with autocast(device.type):
                output = txt2img_pipeline(
                    prompt=request.prompt,
                    num_inference_steps=30,  # Default steps; can adjust as needed TODO FEATURE FLAG
                    guidance_scale=7.5,  # Default guidance scale; can adjust as needed
                    num_images_per_prompt=request.n,
                )
                images = output.images

        id = str(uuid4())

        if request.response_format == "url":
            responses = [
                {
                    "id": id,
                    "object": "image",
                    "created": int(time.time()),
                    "url": await save_image_and_get_url(image, id),
                }
                for image in images
            ]
        else:
            responses = [
                {
                    "id": id,
                    "object": "image",
                    "created": int(time.time()),
                    "b64_json": await encode_image_to_base64(image),
                }
                for image in images
            ]

        return {"data": responses}
    except Exception as e:
        logger.error(f"Error generating images: {e}")
        raise HTTPException(status_code=500, detail="Internal server error.")


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "device": str(device),
        "model": MODEL_NAME,
        "enable_txt2img": ENABLE_TXT2IMG,
        "enable_img2img": ENABLE_IMG2IMG,
    }


# ----------------------
#   METRICS ENDPOINT
# ----------------------
# Initialize Prometheus Instrumentator
Instrumentator().instrument(app).expose(app)
