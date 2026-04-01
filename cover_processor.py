import os
import requests
import logging
import hashlib
import random
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

class CoverProcessor:
    def __init__(self, config):
        self.config = config
        # Baidu Translation API Credentials
        self.appid = config.get('baidu_fanyi', {}).get('appid', '20260331002584381')
        self.security_key = config.get('baidu_fanyi', {}).get('security_key', 'mMtnO6Yz7wXqI5Rhsuj1')
        
    def _call_fanyi(self, text, from_lang='en', to_lang='zh'):
        """Calls the Baidu Translation API (Standard Edition) with MD5 signature."""
        if not text: return ""
        
        url = "http://api.fanyi.baidu.com/api/trans/vip/translate"
        salt = str(random.randint(32768, 65536))
        
        # sign = appid + q + salt + security_key
        sign_str = self.appid + text + salt + self.security_key
        sign = hashlib.md5(sign_str.encode('utf-8')).hexdigest()
        
        params = {
            'q': text,
            'from': from_lang,
            'to': to_lang,
            'appid': self.appid,
            'salt': salt,
            'sign': sign
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            result = response.json()
            if 'trans_result' in result:
                # Return the translated text
                return result['trans_result'][0]['dst']
            else:
                logger.error(f"Baidu Translation API Error: {result}")
                return text # Fallback to original
        except Exception as e:
            logger.error(f"Baidu Translation request failed: {e}")
            return text

    def translate_title(self, title):
        """Translates YouTube title to Chinese using Baidu Translation API."""
        return self._call_fanyi(title)

    def translate_description(self, desc):
        """Translates YouTube description to Chinese using Baidu Translation API."""
        if not desc: return ""
        # Handle large descriptions by translating paragraphs (Baidu limit is 6000 bytes)
        # For simplicity, we just take the first 4000 characters
        return self._call_fanyi(desc[:4000])

    def get_summary(self, title_cn):
        """Extracts 4-6 catchy characters from the Chinese title for the cover."""
        # Since we use simple translation API, we'll take the first 6 characters 
        # or most impactful part of the Chinese title.
        clean_text = "".join(filter(lambda x: '\u4e00' <= x <= '\u9fa5' or x.isalnum(), title_cn))
        if len(clean_text) > 6:
            return clean_text[:6]
        return clean_text if len(clean_text) >= 2 else "精品推荐"

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
            text_img = txt_layer.crop((max(0, x-50), max(0, y-50), 1920, 1080))
            rotated_text = text_img.rotate(10, resample=Image.BICUBIC, expand=True)
            
            # Composite rotated text back
            img.paste(rotated_text, (max(0, x-50), max(0, y-50)), rotated_text)
            
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
