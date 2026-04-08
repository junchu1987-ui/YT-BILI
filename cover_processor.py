import os
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

class CoverProcessor:
    def __init__(self, config):
        self.config = config
        
    def _call_glm(self, messages, max_tokens=512):
        """Calls GLM-4-Flash. Returns response text or None on failure."""
        api_key = self.config.get('zhipu', {}).get('api_key', '')
        if not api_key:
            return None
        try:
            from zhipuai import ZhipuAI
            client = ZhipuAI(api_key=api_key)
            resp = client.chat.completions.create(
                model="glm-4-flash",
                messages=messages,
                max_tokens=max_tokens,
                timeout=15
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"GLM API call failed: {e}")
            return None

    def translate_title(self, title):
        """Translates YouTube title to Chinese using GLM-4-Flash."""
        if not title: return ""
        result = self._call_glm([{
            "role": "user",
            "content": (
                "将以下YouTube视频标题翻译成中文，使用中国大陆日常口语表达，"
                "专有名词用国内常见叫法（如scooter译为电瓶车/摩托车，根据语境判断）。"
                "只输出翻译结果，不要解释：\n"
                f"{title}"
            )
        }], max_tokens=100)
        return result if result else title

    def translate_description(self, desc):
        """Translates YouTube description to Chinese using GLM-4-Flash."""
        if not desc: return ""
        result = self._call_glm([{
            "role": "user",
            "content": f"将以下YouTube视频简介翻译成中文，只输出翻译结果，不要解释：{desc[:3000]}"
        }], max_tokens=1000)
        return result if result else desc

    def get_summary(self, title_cn):
        """Calls GLM-4-Flash to extract 4-6 meaningful Chinese characters from the title."""
        def _fallback(text):
            clean = "".join(c for c in text if '\u4e00' <= c <= '\u9fa5')
            if len(clean) > 6:
                return clean[:6]
            return clean if len(clean) >= 2 else "精品推荐"

        has_chinese = any('\u4e00' <= c <= '\u9fa5' for c in title_cn)
        input_title = title_cn if has_chinese else self.translate_title(title_cn)

        result = self._call_glm([{
            "role": "user",
            "content": (
                "你是一个视频封面文案专家。根据视频标题，提炼一个4到6个汉字的封面标语，"
                "要求语义完整、朗朗上口，字数够用即可不必凑满6字。"
                "只输出标语本身，不要标点、不要解释。\n"
                f"标题：{input_title}"
            )
        }], max_tokens=20)

        if result:
            chinese_only = "".join(c for c in result if '\u4e00' <= c <= '\u9fa5')
            if len(chinese_only) >= 3:
                return chinese_only[:6]

        logger.warning(f"GLM summary insufficient, using fallback for: {input_title}")
        return _fallback(input_title)

    def generate_cover(self, source_image_path, text, output_path):
        """Processes the thumbnail: 1920x1080, 10-degree tilted artistic text at bottom-right."""
        try:
            img = Image.open(source_image_path).convert("RGBA")
            
            # Step 1: Resize to Bilibili standard 1920x1080
            img = img.resize((1920, 1080), Image.Resampling.LANCZOS)
            
            # Step 2: Draw Artistic Text with 10-degree tilt
            font_paths = [
                "C:\\Windows\\Fonts\\msyhbd.ttc", 
                "C:\\Windows\\Fonts\\simhei.ttf"
            ]
            
            font = None
            for fp in font_paths:
                if os.path.exists(fp):
                    try:
                        font = ImageFont.truetype(fp, 260)
                        break
                    except: continue
            if not font: font = ImageFont.load_default()

            # Create a transparent layer for the text
            txt_layer = Image.new("RGBA", (1920, 1080), (255, 255, 255, 0))
            d = ImageDraw.Draw(txt_layer)
            
            # Position at bottom-right
            text_bbox = d.textbbox((0, 0), text, font=font)
            text_w = text_bbox[2] - text_bbox[0]
            text_h = text_bbox[3] - text_bbox[1]
            
            # Initial anchor point for rotation logic
            x = 1920 - text_w - 150
            y = 1080 - text_h - 180
            
            # Draw shadow
            d.text((x+10, y+10), text, font=font, fill=(0, 0, 0, 180))
            # Draw main text
            d.text((x, y), text, font=font, fill=(255, 255, 255, 255))
            
            # Rotate the WHOLE layer by 10 degrees
            # expand=True changes the canvas size, so we rotate the whole layer
            # and then composite it onto the image using alpha compositing.
            rotated_layer = txt_layer.rotate(-10, resample=Image.BICUBIC, expand=False)
            img = Image.alpha_composite(img, rotated_layer)
            
            # Step 3: Save as JPEG for Bilibili
            final_img = img.convert("RGB")
            final_img.save(output_path, "JPEG", quality=90, optimize=True)
            
            if os.path.getsize(output_path) > 2 * 1024 * 1024:
                final_img.save(output_path, "JPEG", quality=75, optimize=True)
                
            logger.info(f"Custom tilted cover generated: {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to generate cover: {e}")
            return False

    def convert_to_jpg(self, source_image_path, output_path):
        """Resize thumbnail to 1920x1080 and save as JPEG without any text overlay."""
        try:
            img = Image.open(source_image_path).convert("RGB")
            img = img.resize((1920, 1080), Image.Resampling.LANCZOS)
            img.save(output_path, "JPEG", quality=90, optimize=True)
            if os.path.getsize(output_path) > 2 * 1024 * 1024:
                img.save(output_path, "JPEG", quality=75, optimize=True)
            logger.info(f"Cover converted to JPG (no text): {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to convert cover to JPG: {e}")
            return False
