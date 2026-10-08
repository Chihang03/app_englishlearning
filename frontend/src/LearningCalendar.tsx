import { useEffect, useState } from "react";
import { errorMessage, isUnauthorized, request } from "./api";
import { watchForeground } from "./foreground";
import { formatStudyTime } from "./studyTime";

type CalendarDay = {
  date: string; reviews: number; completed_cards: number;
  study_time_ms: number | null; first_attempt_accuracy: number | null;
};
type CalendarData = {
  month: string; today: string; days: CalendarDay[];
  summary: { learning_days: number; completed_cards: number };
};

function localToday(timezone: string) {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit"
  }).formatToParts(new Date());
  return ["year", "month", "day"].map(type => parts.find(part => part.type === type)!.value).join("-");
}

function shiftedMonth(month: string, amount: number) {
  const date = new Date(`${month}-01T00:00:00Z`);
  date.setUTCMonth(date.getUTCMonth() + amount);
  return date.toISOString().slice(0, 7);
}

function intensity(day: CalendarDay) {
  if (!day.reviews) return 0;
  if (day.completed_cards < 10) return 1;
  if (day.completed_cards < 30) return 2;
  if (day.completed_cards < 60) return 3;
  return 4;
}

export function LearningCalendar({ timezone, onSignedOut }: {
  timezone: string; onSignedOut: () => void;
}) {
  const [today, setToday] = useState(() => localToday(timezone));
  const [month, setMonth] = useState(() => today.slice(0, 7));
  const [selected, setSelected] = useState(today);
  const [data, setData] = useState<CalendarData | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);

  useEffect(() => watchForeground(() => setRevision(value => value + 1)), []);
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError("");
    void request<CalendarData>(`/api/learning-calendar?month=${month}`, { signal: controller.signal })
      .then(payload => {
        if (controller.signal.aborted) return;
        setToday(payload.today);
        setData(payload);
        setSelected(current => current.startsWith(month) && current <= payload.today
          ? current : payload.today.startsWith(month) ? payload.today : `${month}-01`);
      }).catch(caught => {
        if (controller.signal.aborted) return;
        if (isUnauthorized(caught)) onSignedOut();
        else setError(errorMessage(caught));
      });
    return () => controller.abort();
  }, [month, revision, onSignedOut]);

  const [year, number] = month.split("-").map(Number);
  const offset = (new Date(`${month}-01T00:00:00Z`).getUTCDay() + 6) % 7;
  const day = data?.days.find(item => item.date === selected);
  const label = `${year} 年 ${number} 月`;

  return <div className="learning-calendar">
    <section className="calendar-panel panel" aria-label={label} aria-busy={!data && !error}>
      <div className="calendar-month-heading">
        <button type="button" className="icon-button" aria-label="上个月"
          disabled={month === "0001-01"} onClick={() => setMonth(shiftedMonth(month, -1))}>‹</button>
        <h2 aria-live="polite">{label}</h2>
        <button type="button" className="icon-button" aria-label="下个月"
          disabled={month >= today.slice(0, 7)} onClick={() => setMonth(shiftedMonth(month, 1))}>›</button>
      </div>
      <div className="calendar-weekdays" aria-hidden="true">
        {["一", "二", "三", "四", "五", "六", "日"].map(name => <span key={name}>{name}</span>)}
      </div>
      {error ? <div className="calendar-state" role="alert">
        <p>{error}</p><button type="button" className="text-button" onClick={() => setRevision(value => value + 1)}>重试</button>
      </div> : !data ? <p className="calendar-state" role="status">加载中…</p> : <div className="calendar-grid">
        {Array.from({ length: offset }, (_, index) => <span key={`blank-${index}`} />)}
        {data.days.map(item => <button type="button" key={item.date}
          className={`calendar-day calendar-level-${intensity(item)}${item.date === today ? " is-today" : ""}`}
          aria-label={`${item.date}，完成 ${item.completed_cards} 张卡片`}
          aria-pressed={item.date === selected} aria-current={item.date === today ? "date" : undefined}
          disabled={item.date > today} onClick={() => setSelected(item.date)}>
          {Number(item.date.slice(-2))}
        </button>)}
      </div>}
    </section>
    {data && day ? <>
      <section className="calendar-results panel" aria-live="polite" aria-label="当天成果">
        <h2>{Number(selected.slice(5, 7))} 月 {Number(selected.slice(-2))} 日</h2>
        <dl>
          <div><dt>完成卡片</dt><dd>{day.completed_cards.toLocaleString()} 张</dd></div>
          <div><dt>学习时长</dt><dd>{day.study_time_ms === null ? "—" : formatStudyTime(day.study_time_ms)}</dd></div>
          <div><dt>首次作答正确率</dt><dd>{day.first_attempt_accuracy === null ? "—" : `${day.first_attempt_accuracy}%`}</dd></div>
        </dl>
      </section>
      <section className="calendar-summary panel" aria-label="本月汇总">
        <h2>本月汇总</h2>
        <div><span>学习 <strong>{data.summary.learning_days}</strong> 天</span>
          <span>完成 <strong>{data.summary.completed_cards.toLocaleString()}</strong> 张</span></div>
      </section>
    </> : null}
  </div>;
}
