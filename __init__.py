"""Weather plugin for FiestaBoard.

Displays current weather conditions using WeatherAPI or OpenWeatherMap.
"""

import importlib
import logging
import sys
from typing import Any, Dict, List, Optional

from src.devices import BoardContext, NOTE_COLS
from src.plugins.base import PluginBase, PluginResult
from src.text_to_board import count_tiles, take_tiles

# FiestaBoard reloads the plugin package after an update but can leave child
# modules cached. Refresh the data source as well so new provider fields take
# effect immediately without requiring a full service restart.
_source_module_name = f"{__name__}.source"
if _source_module_name in sys.modules:
    _source_module = importlib.reload(sys.modules[_source_module_name])
else:
    from . import source as _source_module

WeatherSource = _source_module.WeatherSource

logger = logging.getLogger(__name__)

# Board assumed when no board is bound (legacy callers, unit tests). Derived
# from the device registry rather than hand-rolled 22/6 literals.
_DEFAULT_BOARD = BoardContext.from_device_type("flagship")

# Minimum tiles a forecast-day cell needs: 3 (day name) + at least one
# separating space + a temperature ("100F") + a trailing 1-tile colour
# marker. This is the budget the original hardcoded two-column 22-wide
# layout was built around; it now sizes how many side-by-side columns a
# board of any width gets, rather than being a fixed column count.
_MIN_FORECAST_COL_WIDTH = 11


def _fit(cols: int, *candidates: str) -> str:
    """Return the first *candidates* entry that fits within *cols* board tiles.

    Width is measured in tiles, not characters -- a colour marker like
    ``{66}`` is one tile but four characters. If nothing fits (only possible
    on pathologically narrow boards), hard-truncates the last candidate via
    ``take_tiles`` so a colour marker is never split in half.
    """
    for candidate in candidates:
        if count_tiles(candidate) <= cols:
            return candidate
    head, _tail = take_tiles(candidates[-1], cols)
    return head


class WeatherPlugin(PluginBase):
    """Weather data plugin.

    Fetches current weather data from WeatherAPI or OpenWeatherMap
    for one or more configured locations.
    """

    def __init__(self, manifest: Dict[str, Any]):
        """Initialize the weather plugin."""
        super().__init__(manifest)
        self._source: Optional[WeatherSource] = None
        self._cache: Optional[Dict[str, Any]] = None

    @property
    def plugin_id(self) -> str:
        return "weather"

    def validate_config(self, config: Dict[str, Any]) -> List[str]:
        """Validate weather configuration."""
        errors = []

        # Check required fields
        if not config.get("api_key"):
            errors.append("API key is required")

        locations = config.get("locations", [])
        if not locations:
            # Check for legacy single location
            if not config.get("location"):
                errors.append("At least one location is required")

        # Validate provider
        provider = config.get("provider", "weatherapi")
        if provider not in ("weatherapi", "openweathermap"):
            errors.append(f"Invalid provider: {provider}")

        # Validate refresh interval
        refresh = config.get("refresh_seconds", 300)
        if not isinstance(refresh, int) or refresh < 60:
            errors.append("Refresh interval must be at least 60 seconds")

        return errors

    def on_config_change(self, old_config: Dict[str, Any], new_config: Dict[str, Any]) -> None:
        """Handle configuration changes."""
        # Reset source to pick up new config
        self._source = None
        self._cache = None
        logger.debug("Weather source reset due to config change")

    def _get_source(self) -> Optional[WeatherSource]:
        """Get or create the weather source."""
        if self._source is not None:
            return self._source

        config = self.config
        if not config:
            return None

        api_key = config.get("api_key")
        if not api_key:
            return None

        provider = config.get("provider", "weatherapi")

        # Build locations list (support both old and new format)
        locations = config.get("locations", [])
        if not locations:
            # Legacy single location support
            location = config.get("location")
            if location:
                locations = [{"location": location, "name": "HOME"}]

        if not locations:
            return None

        self._source = WeatherSource(
            provider=provider,
            api_key=api_key,
            locations=locations
        )
        return self._source

    def fetch_data(self) -> PluginResult:
        """Fetch weather data for all configured locations."""
        source = self._get_source()

        if source is None:
            return PluginResult(
                available=False,
                error="Weather not configured"
            )

        try:
            # Fetch all locations
            all_data = source.fetch_multiple_locations()

            if not all_data:
                return PluginResult(
                    available=False,
                    error="Failed to fetch weather data"
                )

            # Build response data structure
            # Primary location data (first location) for backward compatibility
            primary = all_data[0]

            data = {
                # Primary location fields (backward compatibility)
                "temperature": primary.get("temperature"),
                "temperature_c": primary.get("temperature_c"),
                "feels_like": primary.get("feels_like"),
                "feels_like_c": primary.get("feels_like_c"),
                "condition": primary.get("condition"),
                "condition_short": primary.get("condition_short"),
                "humidity": primary.get("humidity"),
                "wind_speed": primary.get("wind_speed"),
                "location": primary.get("location"),
                "location_name": primary.get("location_name"),
                # New forecast fields
                "precipitation_chance": primary.get("precipitation_chance"),
                "high_temp": primary.get("high_temp"),
                "high_temp_c": primary.get("high_temp_c"),
                "low_temp": primary.get("low_temp"),
                "low_temp_c": primary.get("low_temp_c"),
                "uv_index": primary.get("uv_index"),
                "sunrise": primary.get("sunrise"),
                "sunset": primary.get("sunset"),
                "next_sun_event": primary.get("next_sun_event"),
                "next_sun_event_time": primary.get("next_sun_event_time"),
                # Aggregate fields
                "location_count": len(all_data),
                # All locations array
                "locations": all_data,
                # Multi-day forecast (from primary location)
                "forecast": primary.get("forecast", []),
            }

            self._cache = data

            return PluginResult(
                available=True,
                data=data
            )

        except Exception as e:
            logger.exception("Error fetching weather data")
            return PluginResult(
                available=False,
                error=str(e)
            )

    def get_formatted_display(self) -> Optional[List[str]]:
        """Return a geometry-aware current-conditions display.

        Adapts to whatever board is bound via ``self.board`` for this render
        (defaulting to a Flagship's 22x6 when unbound -- legacy callers and
        unit tests). Content is ordered by priority and grows to fill a
        taller board with more detail -- feels-like, high/low, UV, next
        sun event, additional configured locations, forecast days -- rather
        than padding blank rows while there is real content left unshown.
        A narrower board abbreviates labels instead of truncating values.
        Rows left over once every available item has been shown are padded
        blank, exactly as a board with nothing more to say should be.
        """
        if not self._cache:
            result = self.fetch_data()
            if not result.available:
                return None

        data = self._cache
        if not data:
            return None

        board = self.board or _DEFAULT_BOARD
        rows, cols = board.rows, board.cols

        lines = self._current_conditions_lines(data, cols)[:rows]
        lines.extend([""] * (rows - len(lines)))
        return lines

    @staticmethod
    def _current_conditions_lines(data: Dict[str, Any], cols: int) -> List[str]:
        """Build the ordered, width-fit content lines for the current-conditions display.

        Each item is skipped (never padded) when its data is missing, so the
        list's length reflects how much there genuinely is to say. That is
        the signal the conformance suite's growth check relies on to tell
        "ran out of content" apart from "capped independently of the board".
        """
        narrow = cols <= NOTE_COLS
        lines: List[str] = []

        location = data.get("location_name")
        header = str(location).upper() if location else "WEATHER"
        lines.append(_fit(cols, header).center(cols))

        temp = data.get("temperature")
        if temp is not None:
            condition = data.get("condition") or "Unknown"
            condition_short = data.get("condition_short") or condition
            primary_text = f"{temp}F {condition_short}" if narrow else f"{temp}° {condition}"
            lines.append(
                _fit(cols, primary_text, f"{temp}° {condition_short}", f"{temp}°").center(cols)
            )

        feels = data.get("feels_like")
        if feels is not None:
            text = f"FEELS {feels}F" if narrow else f"FEELS LIKE {feels}°"
            lines.append(_fit(cols, text).center(cols))

        high = data.get("high_temp")
        low = data.get("low_temp")
        if high is not None or low is not None:
            text = f"HI {high} LO {low}" if narrow else f"HIGH {high}   LOW {low}"
            lines.append(_fit(cols, text).center(cols))

        humidity = data.get("humidity")
        if humidity is not None:
            text = f"HUM {humidity}%" if narrow else f"HUMIDITY {humidity}%"
            lines.append(_fit(cols, text).center(cols))

        wind = data.get("wind_speed")
        if wind is not None:
            lines.append(_fit(cols, f"WIND {wind}MPH").center(cols))

        uv = data.get("uv_index")
        if uv is not None:
            text = f"UV {uv}" if narrow else f"UV INDEX {uv}"
            lines.append(_fit(cols, text).center(cols))

        sun_event = data.get("next_sun_event")
        sun_time = data.get("next_sun_event_time")
        if sun_event and sun_time:
            lines.append(_fit(cols, f"{sun_event} {sun_time}").center(cols))

        # Additional configured locations beyond the primary one -- more of
        # them show up as a taller board makes room, instead of being stuck
        # behind a display that only ever shows location #1.
        for extra in (data.get("locations") or [])[1:]:
            name = extra.get("location_name")
            extra_temp = extra.get("temperature")
            if not name or extra_temp is None:
                continue
            condition = extra.get("condition") or ""
            condition_short = extra.get("condition_short") or condition
            lines.append(
                _fit(
                    cols,
                    f"{name} {extra_temp}F {condition}",
                    f"{name} {extra_temp}F {condition_short}",
                    f"{name} {extra_temp}F",
                ).center(cols)
            )

        # Forecast days, one per line -- likewise only ever shown as far as
        # the board has rows to spare.
        for day in data.get("forecast") or []:
            day_high = day.get("high_temp")
            day_low = day.get("low_temp")
            if day_high is None and day_low is None:
                continue
            day_name = str(day.get("day_name") or "???")[:3]
            if day_high is not None and day_low is not None:
                temps = f"{day_high}/{day_low}"
            else:
                temps = str(day_high if day_high is not None else day_low)
            condition = day.get("condition") or ""
            color = day.get("temperature_color")
            marker = f"{{{color}}}" if color else ""
            lines.append(
                _fit(
                    cols,
                    f"{day_name} {temps} {condition}{marker}",
                    f"{day_name} {temps}{marker}",
                ).center(cols)
            )

        return lines

    def get_forecast_display(self) -> Optional[List[str]]:
        """Return a geometry-aware multi-day forecast display.

        The two-column, four-row layout this used to hardcode only ever fit
        a 22x6 Flagship. Adapts to ``self.board`` (defaulting to a
        Flagship's 22x6 when unbound): the number of side-by-side columns
        and each column's width are derived from ``board.cols`` -- a
        120-wide panel gets ten columns, not two with 98 columns of blank
        space -- and the number of forecast rows from ``board.rows``.
        """
        if not self._cache:
            result = self.fetch_data()
            if not result.available:
                return None

        data = self._cache
        if not data:
            return None

        forecast = data.get("forecast", [])
        if not forecast:
            return None

        board = self.board or _DEFAULT_BOARD
        rows, cols = board.rows, board.cols

        # A board too short for a header/blank pair spends every row on
        # forecast entries instead; one that has six or more rows keeps the
        # original header + blank-separator framing.
        show_header = rows >= 2
        show_blank = rows >= 6
        body_rows = max(rows - int(show_header) - int(show_blank), 0)

        lines: List[str] = []
        if show_header:
            lines.append(
                _fit(
                    cols,
                    "°{violet}{violet} WEATHER REPORT {violet}{violet}°",
                    "WEATHER REPORT",
                )
            )
        if show_blank:
            lines.append("")

        if body_rows == 0:
            return lines

        num_columns = max(1, cols // _MIN_FORECAST_COL_WIDTH)
        col_width = cols // num_columns

        max_days = min(len(forecast), body_rows * num_columns)
        per_column = -(-max_days // num_columns) if max_days else 0  # ceil division
        blank_cell = " " * col_width

        for row_idx in range(body_rows):
            cells = []
            for col_idx in range(num_columns):
                day_idx = col_idx * per_column + row_idx
                if per_column and row_idx < per_column and day_idx < max_days:
                    cells.append(self._format_forecast_entry(forecast[day_idx], col_width))
                else:
                    cells.append(blank_cell)
            lines.append("".join(cells))

        return lines

    @staticmethod
    def _format_forecast_entry(day_data: Dict[str, Any], width: int = 11) -> str:
        """Format a single forecast day to occupy exactly `width` board tiles.

        Layout: day name (3) + padding spaces + temperature + a trailing
        1-tile colour marker, e.g. ``"MON    37F{orange}"`` at the original
        width of 11. `width` is the caller's column budget -- derived from
        the board, not a constant -- so a wider board's multi-column layout
        still lines up tile-for-tile.

        Args:
            day_data: Dict with day_name, high_temp, temperature_color
            width: Board tiles this entry must occupy (default 11, matching
                a Flagship's two-column layout).
        """
        day_name = str(day_data.get("day_name", "???"))[:3]
        temp = day_data.get("high_temp")
        color = day_data.get("temperature_color", "white")

        temp_str = f"{temp}F" if temp is not None else "??F"
        # 3 (day) + spaces + len(temp_str) + 1 (colour marker tile) = width
        spaces = max(1, width - 4 - len(temp_str))
        return f"{day_name}{' ' * spaces}{temp_str}{{{color}}}"

    def cleanup(self) -> None:
        """Clean up resources."""
        self._source = None
        self._cache = None


# Export the plugin class
Plugin = WeatherPlugin
