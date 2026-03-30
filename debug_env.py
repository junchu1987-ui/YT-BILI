import os
import shutil
import subprocess

print(f"Current Working Directory: {os.getcwd()}")
print(f"PATH Environment Variable: {os.environ.get('PATH')}")

for exe in ['node', 'deno', 'bun', 'quickjs']:
    path = shutil.which(exe)
    print(f"shutil.which('{exe}'): {path}")
    if path:
        try:
            ver = subprocess.run([exe, '--version' if exe != 'node' else '-v'], capture_output=True, text=True).stdout.strip()
            print(f"{exe} version: {ver}")
        except Exception as e:
            print(f"Failed to run {exe}: {e}")

# Try finding node in standard location
std_node = r"C:\Program Files\nodejs\node.exe"
if os.path.exists(std_node):
    print(f"Found Node at standard location: {std_node}")
else:
    print("Node NOT found at standard location.")
