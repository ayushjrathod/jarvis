# Wayland audio notes

Pipewire handles both PulseAudio and JACK clients now; sounddevice sees the
default source fine at 16 kHz mono. If the mic level drifts after suspend,
`wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.9` fixes it.

The uinput permission quirk hit again after the last kernel update — had to
re-run `chmod g+rw /dev/uinput` before ydotoold would start.
