import os
import sys
from pathlib import Path

def setup_powershell_profile():
    # Find the ContextOS transcript directory
    base_dir = Path(__file__).resolve().parent.parent.parent
    transcript_dir = base_dir / "data" / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    
    # Define the PowerShell command to start transcription
    # We use a single log file that gets appended to, or we could use daily files.
    # Let's use daily files for easier cleanup, the watcher can handle multiple files.
    ps_command = f"""
# ContextOS Terminal Tracker
$contextos_transcript_path = "{transcript_dir}\\log_$([datetime]::now.ToString('yyyyMMdd')).txt"
if (-not (Test-Path -Path "{transcript_dir}")) {{
    New-Item -ItemType Directory -Force -Path "{transcript_dir}" | Out-Null
}}
try {{
    Start-Transcript -Path $contextos_transcript_path -Append -ErrorAction SilentlyContinue | Out-Null
}} catch {{
    # Ignore if transcript already started
}}
"""
    
    import subprocess
    
    # Ask PowerShell for the true profile path (handles OneDrive etc)
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", "echo $PROFILE"], capture_output=True, text=True, check=True)
        profile_path_str = result.stdout.strip()
        profile_to_use = Path(profile_path_str)
    except Exception as e:
        print(f"Error resolving PowerShell profile path: {e}")
        return
        
    print(f"Targeting PowerShell profile: {profile_to_use}")
    
    # Ensure profile directory exists
    profile_to_use.parent.mkdir(parents=True, exist_ok=True)
    
    # Check if ContextOS is already in the profile
    if profile_to_use.exists():
        with open(profile_to_use, 'r', encoding='utf-8') as f:
            content = f.read()
            if "ContextOS Terminal Tracker" in content:
                print("ContextOS Terminal Tracker is already configured in your PowerShell profile.")
                return

    # Append to profile
    with open(profile_to_use, 'a', encoding='utf-8') as f:
        f.write(ps_command)
        
    print("Successfully added ContextOS Terminal Tracker to your PowerShell profile.")
    print("Please open a new PowerShell window for the changes to take effect.")

if __name__ == "__main__":
    setup_powershell_profile()
