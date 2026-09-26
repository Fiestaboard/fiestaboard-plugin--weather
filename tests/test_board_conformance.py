"""Board-geometry conformance for the Weather plugin.

Renders the plugin across every board shape FiestaBoard supports -- Flagship,
Note, and Note arrays from 15x3 up to 120x24 (which is also what a FiestaPanel
is) -- and asserts it never overflows a row or column, and that its output
actually grows on a taller board rather than staying capped at a Flagship's
fixed 6 rows. See ``src/plugins/geometry_conformance.py`` in FiestaBoard core
for what is checked.
"""

import json
from pathlib import Path
from unittest.mock import Mock

from src.plugins.geometry_conformance import assert_board_conformance

from plugins.weather import WeatherPlugin

MANIFEST = json.loads((Path(__file__).parent.parent / "manifest.json").read_text())


def _forecast_days() -> list:
    """Eight days of forecast data -- enough to exercise growth on a tall board."""
    days = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "MON"]
    colors = ["orange", "violet", "violet", "violet", "violet", "blue", "violet", "violet"]
    return [
        {
            "date": f"2024-01-{index + 1:02d}",
            "day_name": day,
            "high_temp": 50 + index,
            "high_temp_c": 10 + index,
            "low_temp": 40 + index,
            "low_temp_c": 4 + index,
            "condition": "Cloudy",
            "precipitation_chance": 10 * index,
            "temperature_color": color,
        }
        for index, (day, color) in enumerate(zip(days, colors))
    ]


def _locations() -> list:
    """Four configured locations -- the manifest's max -- with rich data on
    each so a tall/wide board has real content to grow into, not just the
    primary location's five fields.
    """
    names = ["HOME", "WORK", "CABIN", "BEACH"]
    return [
        {
            "temperature": 60 + i,
            "temperature_c": 15 + i,
            "feels_like": 62 + i,
            "feels_like_c": 16 + i,
            "condition": "Cloudy",
            "condition_short": "CLOUDY",
            "humidity": 50 + i,
            "wind_speed": 5 + i,
            "location": f"City {i}",
            "location_name": names[i],
            "precipitation_chance": 20,
            "precipitation_chance_today": 20,
            "precipitation_chance_next": 10,
            "high_temp": 65 + i,
            "high_temp_c": 18 + i,
            "low_temp": 50 + i,
            "low_temp_c": 10 + i,
            "uv_index": 5,
            "sunrise": "6:12 AM",
            "sunset": "7:42 PM",
            "next_sun_event": "SET",
            "next_sun_event_time": "7:42 PM",
            # Only the primary location's forecast is surfaced by fetch_data,
            # matching production (it takes all_data[0]).
            "forecast": _forecast_days() if i == 0 else [],
        }
        for i in range(len(names))
    ]


def make_plugin() -> WeatherPlugin:
    """A fresh, configured WeatherPlugin with the network fully stubbed.

    ``_get_source`` normally builds a real ``WeatherSource`` that hits
    WeatherAPI/OpenWeatherMap; here it's replaced with a Mock so the suite
    (which renders this plugin many times, forwards and backwards across
    every geometry) never makes a network call.
    """
    plugin = WeatherPlugin(MANIFEST)
    plugin.config = {
        "api_key": "test_key",
        "provider": "weatherapi",
        "locations": [
            {"location": "San Francisco, CA", "name": "HOME"},
            {"location": "New York, NY", "name": "WORK"},
            {"location": "Lake Tahoe, CA", "name": "CABIN"},
            {"location": "Malibu, CA", "name": "BEACH"},
        ],
    }
    mock_source = Mock()
    mock_source.fetch_multiple_locations.return_value = _locations()
    plugin._source = mock_source
    return plugin


def test_renders_on_every_board_shape():
    """The plugin must fit, and grow into, every board shape the platform supports.

    ``strict_growth=True``: with four locations and an eight-day forecast
    always available, a taller board that fills every row has strictly more
    to show, so a taller board must render more of it -- unlike a clock or a
    single status line, this plugin is not "genuinely capped" content.
    """
    assert_board_conformance(
        make_plugin,
        manifest=MANIFEST,
        strict_growth=True,
        require_note_array_preview=True,
    )
