# Determination of rapid travel velocity

Job runtime estimation needs to know the cutting velocity and rapid travel velocity to work accurately. The `rapid_velocity` parameter in the job configuration specifies the rapid travel velocity.

Rapid travel is the movement of the engraving head when not cutting, i.e. when moving from one cutting position to another while the tool is lifted above the material. Rapid travel velocity is not a configurable parameter on the machine; it has a fixed value determined by the machine's firmware and mechanical limitations.

The process of determining the rapid travel velocity involves simply recording the amount of time it takes to travel a given distance in rapid travel mode. I believe that the speed used when the "GOTO HOME" command is issued corresponds to the rapid travel velocity. Alternatively, you can make a test job that places small cuts at the far-right and right-bottom corner of the plate and the engraving head will rapid travel between them, giving you vertical, horizontal, and diagonal rapid travel times.

## "GOTO HOME" command

First, move the head manually to (24,0) and then use the "GOTO HOME" command to move it to the home position at (what I assume is) rapid travel speed. Repeat that from (0,16) and then again from (24,16).

My measurements:
| From Position (in) | To Position (in) | Distance (in) | Time (s) | Velocity (in/s) |
|------------------|----------------|---------------|----------|----------------| 
| (24,0)           | (0,0)          | 24            | 2.0      | 12.0           |
| (0,16)           | (0,0)          | 16            | 1.5      | 10.67          |
| (24,16)          | (0,0)          | 28.84         | 2.4      | 12.02          |