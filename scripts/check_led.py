"""Quick check: AudioClient LED availability on Jetson."""
import sys

print("=== LED Check ===")

# 1. Check if AudioClient can be imported
try:
    from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
    print("AudioClient import: OK")
    methods = [m for m in dir(AudioClient) if not m.startswith("_")]
    print(f"Methods: {methods}")
    has_led = "LedControl" in methods
    print(f"LedControl method: {'YES' if has_led else 'NO'}")
except ImportError as e:
    print(f"AudioClient import FAILED: {e}")
    has_led = False

# 2. SDK info
try:
    import unitree_sdk2py
    print(f"SDK path: {unitree_sdk2py.__file__}")
    print(f"SDK version: {getattr(unitree_sdk2py, '__version__', 'unknown')}")
except Exception as e:
    print(f"SDK info error: {e}")

# 3. Check audio module
try:
    import unitree_sdk2py.g1.audio as audio_mod
    print(f"audio module dir: {[x for x in dir(audio_mod) if not x.startswith('_')]}")
except ImportError as e:
    print(f"audio module MISSING: {e}")

# 4. Try to actually set LED (needs DDS)
if has_led:
    print("\n--- Attempting LED set (blue) ---")
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        ChannelFactoryInitialize(0, "eth0")
        print("DDS init: OK")
    except ImportError:
        try:
            from unitree_sdk2py.core.channel import ChannelFactory
            ChannelFactory.Instance().Init(0, "eth0")
            print("DDS init (legacy): OK")
        except Exception as e2:
            print(f"DDS init FAILED: {e2}")
            sys.exit(1)
    except Exception as e:
        print(f"DDS init error: {e}")

    try:
        ac = AudioClient()
        ac.SetTimeout(5.0)
        ac.Init()
        print("AudioClient.Init(): OK")
        rc = ac.LedControl(0, 120, 255)
        print(f"LedControl(0,120,255) rc={rc}")
        if rc == 0:
            print("LED SET TO BLUE - SUCCESS!")
        else:
            print(f"LedControl returned non-zero: {rc}")
    except Exception as e:
        print(f"LED control error: {e}")
else:
    # 5. Try alternative: raw API
    print("\n--- Checking alternative LED APIs ---")
    try:
        from unitree_sdk2py.idl.default import unitree_go_msg_dds__SportModeState_
        print("SportModeState available")
    except:
        pass
    try:
        import unitree_sdk2py.g1
        g1_dir = [x for x in dir(unitree_sdk2py.g1) if not x.startswith("_")]
        print(f"g1 submodules: {g1_dir}")
    except:
        pass
    try:
        import pkgutil, unitree_sdk2py.g1 as g1pkg
        subs = [name for importer, name, ispkg in pkgutil.walk_packages(g1pkg.__path__, g1pkg.__name__ + ".")]
        print(f"g1 all submodules: {subs}")
    except Exception as e:
        print(f"pkgutil scan: {e}")

print("\n=== Done ===")
