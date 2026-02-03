# Automation Examples

These examples show how to drive OpenClaw from Home Assistant using the Clawdbot integration services.

## Low battery → notify_event

This sends a structured event into OpenClaw when a battery sensor drops below a threshold.

> Replace `sensor.kitchen_sensor_battery` with your own entity.

```yaml
automation:
  - id: clawdbot_low_battery_notify
    alias: "Clawdbot: low battery -> notify"
    mode: single
    trigger:
      - platform: numeric_state
        entity_id: sensor.kitchen_sensor_battery
        below: 20
    action:
      - service: clawdbot.notify_event
        data:
          event_type: clawdbot.low_battery
          severity: warning
          source: automation.low_battery
          entity_id: sensor.kitchen_sensor_battery
          attributes:
            threshold: 20
            note: "Battery below 20%"
```

Tip: If you want the OpenClaw-side router to forward it into Discord/Telegram, run the router explicitly with the emitted event line.
