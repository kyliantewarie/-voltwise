# Hardware integration and commissioning

## Test without physical hardware

A complete simulator ships with the repository:

```powershell
python -m simulator.runner --cycles 6
python -m pytest tests/test_simulator.py -q
```

It starts a fake Home Assistant REST API, a battery with ramp limits, SOC
cut-offs and efficiency, an EV charger and a grid meter. It exercises real HTTP
actuation, watchdog expiry, rollback, circuit breakers and the closed-loop
deviation check. It is a **functional** substitute, not an electrical model of
a specific inverter — real commissioning is still required.

VoltWise does not speak Modbus or MQTT directly to an inverter. It writes
**setpoints to Home Assistant helper entities**; your HA integration translates
those into device commands. This is deliberate: HA owns the device connection,
permissions and safety interlocks.

## Supported control channels

| Adapter | HA service | Typical use |
|---|---|---|
| `generic` | `number.set_value`, `input_number.set_value` | Any integration exposing a writable number entity |
| `mqtt` | `mqtt.publish` (retained) | ESPHome/Tasmota/custom firmware topics |
| `modbus` | `modbus.write_register` | HA Modbus integration register mapping |

## Minimum commissioning checklist

1. Create the three helper entities that match your adapter:
   - `number.voltwise_battery_power` (kW, signed: + charge / − discharge)
   - `input_number.voltwise_target_soc` (%)
   - `number.voltwise_ev_charge_power` (kW)
2. Wire each helper to your inverter/EV charger integration and **verify the sign
   convention** with a low-power test before enabling automation.
3. Configure the inverter/BMS with its own hard power and SOC limits. VoltWise is
   a setpoint source, not a safety interlock.
4. Set `actuation.enabled: true`, pass `--apply` (and `--confirm` when enabled), then
   run `python -m voltwise --config config.json --validate-only`.
5. Generate the watchdog automation for your exact entity IDs/adapter:
   `python -m voltwise --config config.json --watchdog-automation watchdog.yaml`.
   Merge it into HA and test by setting the expiry helper to a past timestamp.
6. Run the opt-in HIL contract test against your HA instance:
   `VOLTWISE_HIL_HA_URL=http://homeassistant:8123 HA_TOKEN=... python -m pytest tests/test_hardware_live.py`.
7. Observe at least three full cycles with `--dry-run` before enabling writes.

## Unsupported / out of scope
- Three-phase imbalance and per-phase current limits.
- Parallel battery stacks and mixed chemistry packs.
- V2G export and heat-pump thermal storage.
- Firmware-level safety, warranty and cell balancing — always the BMS's job.
