#!/usr/bin/env python3
"""Probe G1 GetActionList (API 7107) — elenco teach/preset sul robot."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from talk_module.robot_actions import _ensure_dds_init

def main():
    from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient

    _ensure_dds_init()
    c = G1ArmActionClient()
    c.Init()
    c.SetTimeout(10.0)
    code, data = c.GetActionList()
    print("code:", code)
    print(json.dumps(data, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
