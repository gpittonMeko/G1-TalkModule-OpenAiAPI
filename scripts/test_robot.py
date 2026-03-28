#!/usr/bin/env python3
"""Quick test: robot action from Jetson."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

print(f"DDS_INTERFACE={os.getenv('UNITREE_DDS_INTERFACE','(not set)')}")
print(f"ROBOT_IP={os.getenv('UNITREE_ROBOT_IP','(not set)')}")

from talk_module.robot_actions import execute_robot_action, _do_arm_action_http
print("--- Trying SDK (face_wave=25) ---")
ok, msg = execute_robot_action(25)
print(f"SDK result: ok={ok} msg={msg}")
print("--- Trying HTTP fallback (face_wave=25) ---")
ok2, msg2 = _do_arm_action_http(25, os.getenv("UNITREE_ROBOT_IP", "192.168.123.161"))
print(f"HTTP result: ok={ok2} msg={msg2}")
