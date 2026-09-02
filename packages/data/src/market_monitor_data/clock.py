from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone

from market_monitor_persistence.database import DatabaseRuntime
from market_monitor_persistence.values import format_rfc3339, parse_rfc3339
from market_monitor_persistence.writer import TransactionContext, WriterQueue

_CHINA = timezone(timedelta(hours=8), "Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class CalendarCoverageReadiness:
    exchange: str
    observed_date: str
    coverage_start: str | None
    coverage_end: str | None
    future_trading_days: int
    minimum_future_trading_days: int
    missing_dates: tuple[str, ...]
    state: str

    @property
    def ready(self) -> bool:
        return self.state == "READY"


class TradingClock:
    def __init__(self, runtime: DatabaseRuntime, writer: WriterQueue) -> None:
        self._runtime = runtime
        self._writer = writer

    def import_day(
        self,
        exchange: str,
        trading_date: str,
        timezone: str,
        sessions: list[tuple[str, str, str]],
    ) -> bool:
        if not exchange.strip() or not timezone.strip():
            raise ValueError("exchange and timezone are required")
        date.fromisoformat(trading_date)
        normalized_sessions = tuple(
            (
                phase,
                format_rfc3339(parse_rfc3339(opens_at)),
                format_rfc3339(parse_rfc3339(closes_at)),
            )
            for phase, opens_at, closes_at in sessions
        )
        if any(not phase.strip() for phase, _, _ in normalized_sessions):
            raise ValueError("session phase is required")
        for _, opens_at, closes_at in normalized_sessions:
            if parse_rfc3339(closes_at) <= parse_rfc3339(opens_at):
                raise ValueError("session close must follow open")

        def command(transaction: TransactionContext) -> bool:
            existing = transaction.connection.exec_driver_sql(
                "SELECT is_trading_day,timezone FROM trading_calendar_day "
                "WHERE exchange=? AND trading_date=?",
                (exchange, trading_date),
            ).one_or_none()
            if existing is not None:
                stored_sessions = tuple(
                    (str(row.phase), str(row.opens_at), str(row.closes_at))
                    for row in transaction.connection.exec_driver_sql(
                        "SELECT phase,opens_at,closes_at FROM trading_session "
                        "WHERE exchange=? AND trading_date=? ORDER BY sequence",
                        (exchange, trading_date),
                    ).all()
                )
                if (
                    int(existing.is_trading_day) != int(bool(normalized_sessions))
                    or str(existing.timezone) != timezone
                    or stored_sessions != normalized_sessions
                ):
                    raise ValueError("calendar day conflicts with existing immutable history")
                return False
            transaction.connection.exec_driver_sql(
                "INSERT INTO trading_calendar_day(exchange,trading_date,is_trading_day,timezone) "
                "VALUES (?,?,?,?)",
                (exchange, trading_date, int(bool(normalized_sessions)), timezone),
            )
            for sequence, (phase, opens_at, closes_at) in enumerate(normalized_sessions):
                transaction.connection.exec_driver_sql(
                    "INSERT INTO trading_session"
                    "(exchange,trading_date,sequence,phase,opens_at,closes_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (exchange, trading_date, sequence, phase, opens_at, closes_at),
                )
            return True

        return bool(self._writer.submit(command).result())

    def phase_at(self, exchange: str, instant: datetime) -> str:
        value = format_rfc3339(instant)
        canonical_instant = parse_rfc3339(value)
        with self._runtime.read_connection() as connection:
            local_date = canonical_instant.date().isoformat()
            day = connection.exec_driver_sql(
                "SELECT is_trading_day FROM trading_calendar_day "
                "WHERE exchange=? AND trading_date=?",
                (exchange, local_date),
            ).scalar_one_or_none()
            if day is None:
                return "CALENDAR_COVERAGE_MISSING"
            if int(day) != 1:
                return "NON_TRADING_DAY"
            sessions = connection.exec_driver_sql(
                "SELECT phase,opens_at,closes_at FROM trading_session "
                "WHERE exchange=? AND trading_date=? ORDER BY sequence",
                (exchange, local_date),
            ).all()
        for row in sessions:
            if (
                parse_rfc3339(str(row.opens_at))
                <= canonical_instant
                < parse_rfc3339(str(row.closes_at))
            ):
                return str(row.phase)
        if sessions and canonical_instant < parse_rfc3339(str(sessions[0].opens_at)):
            return "PRE_OPEN"
        if sessions and canonical_instant >= parse_rfc3339(str(sessions[-1].closes_at)):
            return "CLOSED"
        return "BREAK"

    def calendar_coverage_readiness(
        self,
        exchange: str,
        observed_at: datetime,
        *,
        minimum_future_trading_days: int = 5,
    ) -> CalendarCoverageReadiness:
        """Check explicit daily coverage and the future live-acceptance horizon."""
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("calendar readiness instant must be timezone-aware")
        if minimum_future_trading_days < 0:
            raise ValueError("minimum future trading days must be non-negative")
        observed_date = observed_at.astimezone(_CHINA).date()
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT trading_date,is_trading_day FROM trading_calendar_day "
                "WHERE exchange=? ORDER BY trading_date",
                (exchange,),
            ).all()

        covered = {
            date.fromisoformat(str(row.trading_date)): int(row.is_trading_day) for row in rows
        }
        coverage_start = min(covered, default=None)
        coverage_end = max(covered, default=None)
        missing_dates: list[str] = []
        if coverage_end is None or observed_date > coverage_end:
            missing_dates.append(observed_date.isoformat())
        else:
            current = observed_date
            while current <= coverage_end:
                if current not in covered:
                    missing_dates.append(current.isoformat())
                current += timedelta(days=1)
        future_trading_days = sum(
            is_trading_day
            for trading_date, is_trading_day in covered.items()
            if trading_date > observed_date
        )
        if missing_dates:
            state = "CALENDAR_COVERAGE_MISSING"
        elif future_trading_days < minimum_future_trading_days:
            state = "CALENDAR_COVERAGE_INSUFFICIENT"
        else:
            state = "READY"
        return CalendarCoverageReadiness(
            exchange=exchange,
            observed_date=observed_date.isoformat(),
            coverage_start=None if coverage_start is None else coverage_start.isoformat(),
            coverage_end=None if coverage_end is None else coverage_end.isoformat(),
            future_trading_days=future_trading_days,
            minimum_future_trading_days=minimum_future_trading_days,
            missing_dates=tuple(missing_dates),
            state=state,
        )

    def market_minutes_between(self, exchange: str, start: datetime, end: datetime) -> int:
        """Count whole legal continuous-market minutes on one local trading day."""
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
        ):
            raise ValueError("market-minute bounds must be timezone-aware")
        start_at = parse_rfc3339(format_rfc3339(start))
        end_at = parse_rfc3339(format_rfc3339(end))
        if end_at <= start_at:
            return 0
        trading_date = start_at.astimezone(_CHINA).date().isoformat()
        if end_at.astimezone(_CHINA).date().isoformat() != trading_date:
            raise ValueError("market-minute bounds must use one trading day")
        elapsed = timedelta()
        for _, opens_at, closes_at in self._continuous_sessions(exchange, trading_date):
            overlap_start = max(start_at, opens_at)
            overlap_end = min(end_at, closes_at)
            if overlap_end > overlap_start:
                elapsed += overlap_end - overlap_start
        return int(elapsed.total_seconds() // 60)

    def market_minute_index(self, exchange: str, instant: datetime) -> int | None:
        """Return elapsed legal continuous-market minutes from the day open."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("market-minute instant must be timezone-aware")
        instant_at = parse_rfc3339(format_rfc3339(instant))
        elapsed = timedelta()
        for _, opens_at, closes_at in self._continuous_sessions(
            exchange, instant_at.astimezone(_CHINA).date().isoformat()
        ):
            if instant_at < opens_at:
                return None
            if instant_at <= closes_at:
                return int((elapsed + instant_at - opens_at).total_seconds() // 60)
            elapsed += closes_at - opens_at
        return None

    def final_continuous_close(self, exchange: str, instant: datetime) -> datetime | None:
        """Return the imported final continuous-session close for the instant's day."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("final-close instant must be timezone-aware")
        sessions = self._continuous_sessions(
            exchange, instant.astimezone(_CHINA).date().isoformat()
        )
        return None if not sessions else sessions[-1][2]

    def continuous_session_at(
        self, exchange: str, instant: datetime
    ) -> tuple[str, datetime, datetime] | None:
        """Return the imported continuous session containing an instant, if any."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("continuous-session instant must be timezone-aware")
        for session in self._continuous_sessions(
            exchange, instant.astimezone(_CHINA).date().isoformat()
        ):
            if session[1] <= instant < session[2]:
                return session
        return None

    def latest_completed_continuous_minute(
        self, exchange: str, instant: datetime
    ) -> datetime | None:
        """Return the completed minute right edge in the current continuous session."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("market-minute instant must be timezone-aware")
        instant_at = parse_rfc3339(format_rfc3339(instant))
        session = self.continuous_session_at(exchange, instant_at)
        if session is None:
            return None
        _, opens_at, closes_at = session
        minute_end = instant_at.replace(second=0, microsecond=0)
        if minute_end <= opens_at or minute_end > closes_at:
            return None
        return minute_end

    def latest_completed_legal_minute(self, exchange: str, instant: datetime) -> datetime | None:
        """Return the latest completed minute right edge, including breaks/close."""
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("market-minute instant must be timezone-aware")
        instant_at = parse_rfc3339(format_rfc3339(instant))
        candidate: datetime | None = None
        for _, opens_at, closes_at in self._continuous_sessions(
            exchange, instant_at.astimezone(_CHINA).date().isoformat()
        ):
            completed_until = min(instant_at, closes_at)
            if completed_until <= opens_at:
                continue
            minute_end = completed_until.replace(second=0, microsecond=0)
            if opens_at < minute_end <= closes_at:
                candidate = minute_end
            if instant_at < closes_at:
                break
        return candidate

    def completed_legal_minute_tail(
        self, exchange: str, target: datetime, count: int
    ) -> tuple[datetime, ...]:
        """Return up to ``count`` same-day completed legal 1m right-edge labels."""
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError("market-minute target must be timezone-aware")
        if count < 1:
            raise ValueError("market-minute tail count must be positive")
        target_at = parse_rfc3339(format_rfc3339(target))
        if target_at != target_at.replace(second=0, microsecond=0):
            raise ValueError("market-minute target must be a whole-minute right edge")
        if self.latest_completed_legal_minute(exchange, target_at) != target_at:
            raise ValueError("market-minute target must be a completed legal right edge")
        values: list[datetime] = []
        for _, opens_at, closes_at in reversed(
            self._continuous_sessions(exchange, target_at.astimezone(_CHINA).date().isoformat())
        ):
            current = min(target_at, closes_at)
            while current > opens_at and len(values) < count:
                values.append(current)
                current -= timedelta(minutes=1)
            if len(values) == count:
                break
        return tuple(reversed(values))

    def _continuous_sessions(
        self, exchange: str, trading_date: str
    ) -> tuple[tuple[str, datetime, datetime], ...]:
        with self._runtime.read_connection() as connection:
            rows = connection.exec_driver_sql(
                "SELECT phase,opens_at,closes_at FROM trading_session "
                "WHERE exchange=? AND trading_date=? "
                "AND phase IN ('CONTINUOUS_AM','CONTINUOUS_PM') ORDER BY sequence",
                (exchange, trading_date),
            ).all()
        return tuple(
            (
                str(row.phase),
                parse_rfc3339(str(row.opens_at)),
                parse_rfc3339(str(row.closes_at)),
            )
            for row in rows
        )

    def tdx_quote_time(
        self, exchange: str, received_at: datetime, server_time_raw: int
    ) -> str | None:
        """Return a canonical TDX source time only when its market session is known."""
        if server_time_raw < 0:
            return None
        if server_time_raw > 235_959:
            hours, remainder = divmod(server_time_raw, 1_000_000)
            minutes, remainder = divmod(remainder, 10_000)
            seconds, centiseconds = divmod(remainder, 100)
        else:
            hours, remainder = divmod(server_time_raw, 10_000)
            minutes, seconds = divmod(remainder, 100)
            centiseconds = 0
        if not (0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59):
            return None
        local_date = received_at.astimezone(_CHINA).date()
        instant = datetime.combine(
            local_date,
            time(hours, minutes, seconds, centiseconds * 10_000),
            _CHINA,
        ).astimezone(UTC)
        if self.phase_at(exchange, instant) not in {"CONTINUOUS_AM", "CONTINUOUS_PM"}:
            return None
        return format_rfc3339(instant)

    def previous_valid_trading_dates(
        self, exchange: str, before: str, count: int
    ) -> tuple[str, ...]:
        """Return up to ``count`` imported valid dates strictly before ``before``."""
        if count < 0:
            raise ValueError("count must be non-negative")
        date.fromisoformat(before)
        with self._runtime.read_connection() as connection:
            rows = (
                connection.exec_driver_sql(
                    "SELECT trading_date FROM trading_calendar_day "
                    "WHERE exchange=? AND is_trading_day=1 AND trading_date<? "
                    "ORDER BY trading_date DESC LIMIT ?",
                    (exchange, before, count),
                )
                .scalars()
                .all()
            )
        return tuple(reversed(tuple(str(value) for value in rows)))

    @staticmethod
    def validate_source_time(value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return format_rfc3339(parse_rfc3339(value))
        except ValueError:
            return None
