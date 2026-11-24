"""Constants for the VoltWise integration."""

DOMAIN = "voltwise"
PLATFORMS = ["sensor"]

BATTERY_POWER_ENTITY = "number.voltwise_battery_power"
TARGET_SOC_ENTITY = "input_number.voltwise_target_soc"
EV_POWER_ENTITY = "number.voltwise_ev_charge_power"
EXPIRY_ENTITY = "input_datetime.voltwise_plan_expiry"
COST_ENTITY = "input_number.voltwise_total_cost"
SAVINGS_ENTITY = "input_number.voltwise_savings"
STATUS_ENTITY = "input_number.voltwise_status_code"
