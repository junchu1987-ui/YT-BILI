import os
import pytest
from cover_processor import CoverProcessor

# Mock config for testing
test_config = {
    'baidu_ai': {
        'api_key': 'test_key',
        'secret_key': 'test_secret'
    }
}

def test_cover_generation_basic():
    """Tests if the cover processor can resize and draw text without crashing."""
    processor = CoverProcessor(test_config)
    
    # Create a dummy white image
    from PIL import Image
    dummy_input = "test_input.jpg"
    dummy_output = "test_output.jpg"
    
    img = Image.new('RGB', (100, 100), color=(255, 255, 255))
    img.save(dummy_input)
    
    try:
        success = processor.generate_cover(dummy_input, "测试", dummy_output)
        assert success is True
        assert os.path.exists(dummy_output)
        
        # Verify size
        with Image.open(dummy_output) as final_img:
            assert final_img.size == (1920, 1080)
    finally:
        # Cleanup
        if os.path.exists(dummy_input): 
            try: os.remove(dummy_input)
            except: pass
        if os.path.exists(dummy_output): 
            try: os.remove(dummy_output)
            except: pass

def test_summary_fallback():
    """Tests if the summary returns a fallback when API keys are invalid."""
    processor = CoverProcessor(test_config)
    summary = processor.get_summary("This is a test title")
    assert summary == "必刷" or len(summary) > 0
