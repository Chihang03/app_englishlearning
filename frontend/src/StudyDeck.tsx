import { useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, PointerEvent, ReactNode } from "react";
import type { Card, ReviewResult } from "./types";

export type PreviousQuestion = { card: Card; result: ReviewResult; showTranslation: boolean };

type Props = {
  currentId: string | null;
  previous: PreviousQuestion | null;
  reviewing: boolean;
  blocked: boolean;
  canAdvance: boolean;
  previousContent: ReactNode;
  children: ReactNode;
  onReviewChange: (reviewing: boolean) => void;
  onReviewReady: () => void;
  onAdvance: () => void;
  onMotionChange: (moving: boolean) => void;
  onInteraction: () => void;
};

const MOTION_MS = 250;

// The live question has one stable DOM subtree, including its textarea. Only
// the previous question is a snapshot; changing the view never changes a round.
export function StudyDeck(props: Props) {
  const { previous, reviewing, blocked, canAdvance } = props;
  const latest = useRef(props);
  latest.current = props;
  const viewport = useRef<HTMLDivElement>(null);
  const currentId = useRef(props.currentId);
  const timer = useRef<number | undefined>(undefined);
  const moving = useRef(false);
  const gesture = useRef<{ id: number; x: number; y: number; time: number; horizontal: boolean } | null>(null);
  const [offset, setOffset] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [advancing, setAdvancing] = useState(false);
  const [settling, setSettling] = useState(false);
  const interactive = !blocked && !advancing && !settling;

  function motion(done?: () => void) {
    window.clearTimeout(timer.current);
    moving.current = true;
    latest.current.onMotionChange(true);
    const duration = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : MOTION_MS;
    timer.current = window.setTimeout(() => {
      moving.current = false;
      setAdvancing(false);
      setSettling(false);
      latest.current.onMotionChange(false);
      done?.();
    }, duration);
  }

  useLayoutEffect(() => {
    if (currentId.current === props.currentId) return;
    const oldId = currentId.current;
    currentId.current = props.currentId;
    gesture.current = null;
    setOffset(0);
    setDragging(false);
    if (oldId !== null && props.previous?.card.attempt_id === oldId) {
      setAdvancing(true);
      motion();
    }
  }, [props.currentId]);

  useLayoutEffect(() => () => {
    window.clearTimeout(timer.current);
    latest.current.onMotionChange(false);
  }, []);

  function changeView(next: boolean) {
    if (blocked || moving.current || (next && !previous)) return;
    latest.current.onInteraction();
    setOffset(0);
    setDragging(false);
    setSettling(true);
    latest.current.onReviewChange(next);
    motion(() => {
      if (next && latest.current.reviewing) latest.current.onReviewReady();
    });
  }

  function pointerDown(event: PointerEvent<HTMLDivElement>) {
    if (!event.isPrimary || event.button !== 0 || blocked || moving.current) return;
    const target = event.target as HTMLElement;
    if (target.closest("input, textarea, button, a, select, summary, [contenteditable='true']") || window.getSelection()?.toString()) return;
    gesture.current = { id: event.pointerId, x: event.clientX, y: event.clientY, time: event.timeStamp, horizontal: false };
  }

  function pointerMove(event: PointerEvent<HTMLDivElement>) {
    const start = gesture.current;
    if (!start || start.id !== event.pointerId) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (!start.horizontal) {
      if (Math.abs(dy) > 10 && Math.abs(dy) >= Math.abs(dx)) {
        gesture.current = null;
        return;
      }
      if (Math.abs(dx) < 10 || Math.abs(dx) < Math.abs(dy) * 1.3) return;
      start.horizontal = true;
      event.currentTarget.setPointerCapture(event.pointerId);
      setDragging(true);
      latest.current.onMotionChange(true);
      latest.current.onInteraction();
    }
    const allowed = reviewing ? dx < 0 : (dx > 0 && Boolean(previous)) || (dx < 0 && canAdvance);
    // Fetch the next round only after release; incomplete answers and the
    // oldest retained card resist dragging at their boundaries.
    const width = viewport.current?.clientWidth ?? 300;
    setOffset(allowed ? Math.max(-width, Math.min(width, dx)) : Math.sign(dx) * Math.min(48, Math.abs(dx) * .18));
  }

  function pointerEnd(event: PointerEvent<HTMLDivElement>, cancelled = false) {
    const start = gesture.current;
    if (!start || start.id !== event.pointerId) return;
    gesture.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (!start.horizontal) return;
    const dx = event.clientX - start.x;
    const width = viewport.current?.clientWidth ?? 300;
    const commit = !cancelled && (Math.abs(dx) > width * .25 || (Math.abs(dx) > 35 && Math.abs(dx) / Math.max(1, event.timeStamp - start.time) > .5));
    setDragging(false);
    setOffset(0);
    if (commit && ((reviewing && dx < 0) || (!reviewing && dx > 0 && previous))) {
      changeView(!reviewing);
    } else {
      setSettling(true);
      motion();
      if (commit && !reviewing && dx < 0 && canAdvance) latest.current.onAdvance();
    }
  }

  const style = { "--drag-x": `${offset}px` } as CSSProperties;
  return <div className="study-deck">
    <div ref={viewport} style={style}
      className={`study-deck-viewport${reviewing ? " is-reviewing" : ""}${dragging ? " is-dragging" : ""}${advancing ? " is-advancing" : ""}${settling ? " is-settling" : ""}`}
      onPointerDown={pointerDown} onPointerMove={pointerMove}
      onPointerUp={pointerEnd} onPointerCancel={(event) => pointerEnd(event, true)}
      onLostPointerCapture={(event) => {
        // Touch starts with implicit capture on the child under the finger.
        // Transferring it to this viewport also bubbles the child's loss event.
        if (event.target === event.currentTarget && gesture.current) pointerEnd(event, true);
      }}>
      <div className="study-deck-current" inert={reviewing} aria-hidden={reviewing || undefined}>
        {props.children}
      </div>
      {previous ? <div className="study-deck-previous" inert={!reviewing} aria-hidden={!reviewing || undefined}>
        {props.previousContent}
      </div> : null}
    </div>
    <div className="study-deck-navigation sr-only" aria-label="切换学习卡片">
      {reviewing ? <button type="button" disabled={!interactive} onClick={() => changeView(false)}>返回当前题 →</button> : <>
        <button type="button" disabled={!previous || !interactive} onClick={() => changeView(true)}>← 上一题</button>
        {canAdvance ? <button type="button" disabled={!interactive} onClick={props.onAdvance}>下一题 →</button> : null}
      </>}
    </div>
    <span className="sr-only" role="status">{reviewing ? "正在回看上一题，左滑返回当前题" : "当前题目"}</span>
  </div>;
}
