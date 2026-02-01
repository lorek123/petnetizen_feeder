"""
Petnetizen Feeder BLE Library

A Python library for controlling Petnetizen automatic pet feeders via Bluetooth Low Energy.
Based on reverse engineering of the official Android app.

Example usage:
    from petnetizen_feeder import FeederDevice
    
    async def main():
        feeder = FeederDevice("E6:C0:07:09:A3:D3")
        await feeder.connect()
        
        # Manual feed with 2 portions
        await feeder.feed(portions=2)
        
        # Set schedule: 8:00 AM every day, 1 portion
        await feeder.set_schedule([
            {"weekdays": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], 
             "time": "08:00", "portions": 1, "enabled": True}
        ])
        
        # Toggle child lock
        await feeder.set_child_lock(False)  # Unlock
        
        # Toggle sound
        await feeder.set_sound(True)  # Enable
        
        await feeder.disconnect()
    
    asyncio.run(main())
"""

from .feeder import FeederDevice, FeedSchedule, Weekday

__version__ = "0.1.0"
__all__ = ["FeederDevice", "FeedSchedule", "Weekday"]
