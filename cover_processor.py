import os
import requests
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

class CoverProcessor:
    def __init__(self, config):
        self.config = config
        self.api_key = config.get('baidu_ai', {}).get('api_key')
        self.secret_key = config.get('baidu_ai', {}).get('secret_key')
        self.access_token = None

    def _get_access_token(self):
        """Fetches the access token for Baidu AI Cloud."""
        if self.access_token:
            return self.access_token
        
        url = f"https://aip.baidubce.com/oauth/2.0/token?grant_type=client_credentials&client_id={self.api_key}&client_secret={self.secret_key}"
        try:
            response = requests.get(url)
            if response.status_code == 200:
                self.access_token = response.json().get("access_token")
                return self.access_token
        except Exception as e:
            logger.error(f"Failed to get Baidu access token: {e}")
        return None

    def get_summary(self, title, description=""):
        """Uses Baidu ERNIE Bot to summarize the video title into 1-2 powerful words."""
        token = self._get_access_token()
        if not token:
            return "精彩视频"

        url = f"https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat/eb-marginal?access_token={token}"
        
        prompt = f"请根据视频标题：'{title}'，给出一个1到2个字的超短中文关键词，要求极简、抓人眼球，用于视频封面，只返回这两个字，不要任何标点或多余文字。"
        
        payload = {
            "messages": [{"role": "user", "content": prompt}]
        }
        
        try:
            response = requests.post(url, json=payload)
            if response.status_code == 200:
                result = response.json().get("result", "")
                summary = result.strip().replace("。", "").replace("\"", "")[:3]
                logger.info(f"Baidu LLM summary: {summary}")
                return summary if summary else "必刷"
        except Exception as e:
            logger.error(f"Baidu LLM request failed: {e}")
        
        return "必刷"

    def generate_cover(self, source_image_path, text, output_path):
        """Processes the thumbnail: resizes, adds artistic text, and optimizes size."""
        try:
            img = Image.open(source_image_path).convert("RGB")
            
            # Step 1: Resize to Bilibili standard 1920x1080
            img = img.resize((1920, 1080), Image.Resampling.LANCZOS)
            draw = ImageDraw.Draw(img)
            
            # Step 2: Draw Artistic Text
            # Try to find a nice font
            font_paths = [
                "C:\\Windows\\Fonts\\msyhbd.ttc", # Microsoft YaHei Bold
                "C:\\Windows\\Fonts\\simhei.ttf", # SimHei
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" # Linux fallback
            ]
            
            font = None
            for fp in font_paths:
                if os.path.exists(fp):
                    try:
                        font = ImageFont.truetype(fp, 260)
                        break
                    except: continue
            
            if not font:
                font = ImageFont.load_default()

            # Draw Text with Shadow/Stroke for "Artistic" effect
            # Position: bottom right
            text_bbox = draw.textbbox((0, 0), text, font=font)
            text_w = text_bbox[2] - text_bbox[0]
            text_h = text_bbox[3] - text_bbox[1]
            
            x = 1920 - text_w - 100
            y = 1080 - text_h - 150
            
            # Draw shadow
            draw.text((x+8, y+8), text, font=font, fill=(0, 0, 0, 150))
            # Draw main text (Bilibili Pink or Vibrant Yellow/White)
            draw.text((x, y), text, font=font, fill=(255, 255, 255))
            
            # Step 3: Save and optimize (ensure < 2MB)
            img.save(output_path, "JPEG", quality=90, optimize=True)
            
            # Check size, if > 2MB, reduce quality
            if os.path.getsize(output_path) > 2 * 1024 * 1024:
                img.save(output_path, "JPEG", quality=75, optimize=True)
                
            logger.info(f"Custom cover generated at: {output_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to generate cover: {e}")
            return False
