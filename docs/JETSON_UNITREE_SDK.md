# Jetson: Unitree SDK2 Python (braccia G1 + LocoClient)

Sul **computer di bordo (Jetson)** del G1 servono **Cyclone DDS** e **`unitree_sdk2py`** perché `talk_module/robot_actions.py` usi:

- `G1ArmActionClient` (azioni braccia, API 7106)
- `LocoClient` (Ready / Walk / joystick `Move`, ecc.)

L’app Talk gira in `.venv` sotto `~/G1-TalkModule-OpenAiAPI`.

---

## Installazione automatica (consigliata)

Sul Jetson, dopo aver creato la venv del progetto (`install.sh` / `scripts/install_jetson_tutto_in_cartella.sh`):

```bash
cd ~/G1-TalkModule-OpenAiAPI
bash scripts/install_unitree_sdk_jetson.sh
bash scripts/restart_server.sh
```

Lo script:

1. Mostra lo spazio disco (`df`).
2. **Compila Cyclone DDS 0.10.x (C)** in `$HOME/cyclonedds-install` (solo se manca `libddsc.so`). Rimuove la cartella `build` dopo l’install per limitare spazio; i sorgenti restano in `$HOME/.cache/cyclonedds-0.10-src` (eliminabili con `rm -rf` se serve).
3. Installa **`cyclonedds==0.10.2`** (binding Python) usando `CYCLONEDDS_HOME` verso quell’install prefix.
4. Clona **unitree_sdk2_python** in cache, aggiunge `__init__.py` vuoti sotto `unitree_sdk2py/g1/…` (vedi sotto), esegue `pip install .`, poi elimina il clone.
5. Applica **`scripts/patch_unitree_sdk2py_init.sh`**: il `__init__.py` upstream importa `b2` e può causare **import circolare**; per uso G1 non serve `b2`.
6. Verifica: `from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient`.

### Variabili opzionali

| Variabile | Default | Uso |
|-----------|---------|-----|
| `CYCLONEDDS_INSTALL` | `$HOME/cyclonedds-install` | Prefix installazione libreria C Cyclone DDS |
| `CYCLONE_SRC` | `$HOME/.cache/cyclonedds-0.10-src` | Clone sorgenti Cyclone (solo in fase di build) |
| `UNITREE_SDK2_SRC` | `$HOME/.cache/unitree_sdk2_python-src` | Clone temporaneo prima di `pip install .` (rimosso a fine script) |

---

## Perché non basta `pip install` da PyPI

1. **`cyclonedds==0.10.2` su PyPI**: per `linux_aarch64` non c’è wheel ufficiale; serve la **libreria C** compilata e `CYCLONEDDS_HOME` (o si ottiene l’errore *Could not locate cyclonedds*).
2. **Wheel `unitree_sdk2py` da solo**: `setuptools` con `find_packages` **non include** i moduli sotto `g1/` perché upstream non mette `__init__.py` in `g1`, `g1/arm`, `g1/loco`, `g1/audio`. Lo script aggiunge quei file **solo in fase di build** dal clone Git, così nel site-packages risultano `unitree_sdk2py.g1.loco`, ecc.
3. **Patch `__init__`**: dopo ogni reinstall di `unitree_sdk2py`, rieseguire `bash scripts/patch_unitree_sdk2py_init.sh` (lo fa già `install_unitree_sdk_jetson.sh`).

---

## Verifica manuale

```bash
cd ~/G1-TalkModule-OpenAiAPI
.venv/bin/python3 scripts/verify_unitree_loco_import.py
```

---

## Dopo aggiornamenti SDK o `pip install --force-reinstall`

1. `bash scripts/install_unitree_sdk_jetson.sh` (salta la build C se già presente).
2. `bash scripts/restart_server.sh`.

---

## Spazio disco e pulizia

- In genere il Jetson G1 ha molto spazio su NVMe; lo script stampa `df` prima/dopo.
- Per recuperare spazio: `rm -rf ~/.cache/cyclonedds-0.10-src` (solo se non devi ricompilare Cyclone).

---

## Rete e runtime DDS

`robot_actions.py` chiama **`ChannelFactoryInitialize(0, iface)`** (API ufficiale SDK: il singleton non espone `.Instance()`). L’interfaccia di default è **`eth0`**; sovrascrivibile con env **`UNITREE_DDS_INTERFACE`**. In sport mode e rete corretta, LocoClient e arm client parlano al G1 via DDS.

---

## Script correlati in repo

| File | Ruolo |
|------|--------|
| `scripts/install_unitree_sdk_jetson.sh` | Installazione completa Cyclone C + pip + patch |
| `scripts/patch_unitree_sdk2py_init.sh` | Patch post-install `unitree_sdk2py/__init__.py` |
| `scripts/verify_unitree_loco_import.py` | Test import `LocoClient` |
| `scripts/restart_server.sh` | Riavvio modulo Talk dopo modifiche |

---

## Documentazione azioni vocali / web

- Comandi e config: [ROBOT_ACTIONS.md](ROBOT_ACTIONS.md)
- Installazione generale Jetson: [INSTALLAZIONE.md](INSTALLAZIONE.md)
