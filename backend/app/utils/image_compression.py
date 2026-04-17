"""
Image compression utilities for handling screenshots efficiently
"""

import base64
import io
import logging
from typing import Optional, Tuple
from PIL import Image
import hashlib

logger = logging.getLogger(__name__)

class ImageCompressor:
    """Handle image compression and optimization for screenshots"""
    
    _cache = {}
    MAX_CACHE_SIZE = 50
    
    @staticmethod
    def compress_screenshot(
        base64_data: str,
        max_width: int = 1280,
        max_height: int = 720,
        quality: int = 65,
        format: str = "JPEG"
    ) -> Tuple[str, int, int]:
        """
        Compress a base64 encoded screenshot to reduce size
        
        Returns:
            Tuple of (compressed_base64, original_size, compressed_size)
        """
        try:
            cache_key = hashlib.md5(f"{base64_data[:100]}_{max_width}_{max_height}_{quality}".encode()).hexdigest()
            
            if cache_key in ImageCompressor._cache:
                logger.debug(f"Using cached compressed image for key {cache_key}")
                return ImageCompressor._cache[cache_key]
            
            if base64_data.startswith("data:image"):
                base64_data = base64_data.split(",", 1)[1]
            
            image_bytes = base64.b64decode(base64_data)
            original_size = len(image_bytes)
            
            img = Image.open(io.BytesIO(image_bytes))
            
            if format == "JPEG" and img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            
            width, height = img.size
            aspect_ratio = width / height
            
            if width > max_width or height > max_height:
                if width / max_width > height / max_height:
                    new_width = max_width
                    new_height = int(max_width / aspect_ratio)
                else:
                    new_height = max_height
                    new_width = int(max_height * aspect_ratio)
                
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                logger.debug(f"Resized image from {width}x{height} to {new_width}x{new_height}")
            
            output_buffer = io.BytesIO()
            save_kwargs = {"format": format}
            
            if format == "JPEG":
                save_kwargs["quality"] = quality
                save_kwargs["optimize"] = True
                save_kwargs["progressive"] = True
            elif format == "PNG":
                save_kwargs["optimize"] = True
                save_kwargs["compress_level"] = 9
            
            img.save(output_buffer, **save_kwargs)
            
            compressed_bytes = output_buffer.getvalue()
            compressed_size = len(compressed_bytes)
            compressed_base64 = base64.b64encode(compressed_bytes).decode('utf-8')
            
            mime_type = "jpeg" if format == "JPEG" else "png"
            compressed_with_prefix = f"data:image/{mime_type};base64,{compressed_base64}"
            
            compression_ratio = (1 - compressed_size / original_size) * 100
            logger.info(f"Screenshot compressed: {original_size:,} -> {compressed_size:,} bytes ({compression_ratio:.1f}% reduction)")
            
            if len(ImageCompressor._cache) >= ImageCompressor.MAX_CACHE_SIZE:
                oldest_keys = list(ImageCompressor._cache.keys())[:10]
                for key in oldest_keys:
                    del ImageCompressor._cache[key]
            
            result = (compressed_with_prefix, original_size, compressed_size)
            ImageCompressor._cache[cache_key] = result
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to compress screenshot: {str(e)}")
            if not base64_data.startswith("data:image"):
                base64_data = f"data:image/png;base64,{base64_data}"
            return base64_data, len(base64_data), len(base64_data)
    
    @staticmethod
    def extract_thumbnail(
        base64_data: str,
        thumbnail_size: Tuple[int, int] = (320, 180)
    ) -> Optional[str]:
        """Extract a small thumbnail from the screenshot for quick preview"""
        try:
            if base64_data.startswith("data:image"):
                base64_data = base64_data.split(",", 1)[1]
            
            image_bytes = base64.b64decode(base64_data)
            img = Image.open(io.BytesIO(image_bytes))
            
            img.thumbnail(thumbnail_size, Image.Resampling.LANCZOS)
            
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            
            output_buffer = io.BytesIO()
            img.save(output_buffer, format="JPEG", quality=50, optimize=True)
            
            thumbnail_bytes = output_buffer.getvalue()
            thumbnail_base64 = base64.b64encode(thumbnail_bytes).decode('utf-8')
            
            return f"data:image/jpeg;base64,{thumbnail_base64}"
            
        except Exception as e:
            logger.error(f"Failed to create thumbnail: {str(e)}")
            return None
    
    @staticmethod
    def compress_raw_base64(
        base64_data: str,
        max_width: int = 1280,
        max_height: int = 720,
        quality: int = 65
    ) -> str:
        """Compress raw base64 (without data URI prefix) and return raw base64"""
        try:
            if base64_data.startswith("data:image"):
                base64_data = base64_data.split(",", 1)[1]
            
            image_bytes = base64.b64decode(base64_data)
            img = Image.open(io.BytesIO(image_bytes))
            
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            
            width, height = img.size
            aspect_ratio = width / height
            
            if width > max_width or height > max_height:
                if width / max_width > height / max_height:
                    new_width = max_width
                    new_height = int(max_width / aspect_ratio)
                else:
                    new_height = max_height
                    new_width = int(max_height * aspect_ratio)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            output_buffer = io.BytesIO()
            img.save(output_buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
            
            compressed_base64 = base64.b64encode(output_buffer.getvalue()).decode('utf-8')
            return compressed_base64
            
        except Exception as e:
            logger.error(f"Failed to compress raw base64: {str(e)}")
            return base64_data
    
    @staticmethod
    def clear_cache():
        """Clear the compression cache"""
        ImageCompressor._cache.clear()
        logger.info("Image compression cache cleared")
