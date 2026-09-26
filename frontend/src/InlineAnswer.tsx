import { forwardRef, useCallback, useLayoutEffect, useRef } from "react";
import type { ComponentPropsWithoutRef } from "react";

type Props = Omit<ComponentPropsWithoutRef<"textarea">, "value" | "rows" | "style"> & {
  value: string;
  onEnter?: () => void;
};

// A stable textarea keeps focus across cards and can wrap a word that exceeds
// a whole sentence line. Both measurement spans inherit the sentence's font.
export const InlineAnswer = forwardRef<HTMLTextAreaElement, Props>(function InlineAnswer(
  { value, onEnter, onKeyDown, onCompositionStart, onCompositionEnd, ...props }, forwardedRef
) {
  const blankRef = useRef<HTMLSpanElement>(null);
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const textRef = useRef<HTMLSpanElement>(null);
  const minimumRef = useRef<HTMLSpanElement>(null);
  const composingRef = useRef(false);

  const setField = useCallback((field: HTMLTextAreaElement | null) => {
    fieldRef.current = field;
    if (typeof forwardedRef === "function") forwardedRef(field);
    else if (forwardedRef) forwardedRef.current = field;
  }, [forwardedRef]);

  const resize = useCallback(() => {
    const blank = blankRef.current;
    const field = fieldRef.current;
    const sentence = blank?.parentElement;
    if (!blank || !field || !sentence || sentence.clientWidth === 0) return;

    const style = window.getComputedStyle(field);
    const horizontalPadding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
    const horizontalBorder = parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
    const verticalBorder = parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    const textWidth = textRef.current?.getBoundingClientRect().width ?? 0;
    const minimumWidth = minimumRef.current?.getBoundingClientRect().width ?? 0;
    // Leave room for the caret and glyph edges; never limit by character count.
    const desiredWidth = Math.ceil(Math.max(minimumWidth, textWidth) + horizontalPadding + horizontalBorder + 8);
    blank.style.width = `${Math.min(sentence.clientWidth - 4, desiredWidth)}px`;
    field.style.height = "0px";
    field.style.height = `${Math.ceil(field.scrollHeight + verticalBorder + 1)}px`;
  }, []);

  useLayoutEffect(resize, [resize, value, props.className]);

  useLayoutEffect(() => {
    const sentence = blankRef.current?.parentElement;
    if (!sentence) return;
    let active = true;
    let previousWidth = -1;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width === previousWidth) return;
      previousWidth = entry.contentRect.width;
      resize();
    });
    observer.observe(sentence);
    void document.fonts.ready.then(() => { if (active) resize(); });
    document.fonts.addEventListener("loadingdone", resize);
    return () => {
      active = false;
      observer.disconnect();
      document.fonts.removeEventListener("loadingdone", resize);
    };
  }, [resize]);

  return (
    <span ref={blankRef} className="sentence-blank">
      <span className="sentence-measurer" aria-hidden="true">
        <span ref={textRef} className="sentence-measure">{value}</span>
        <span ref={minimumRef} className="sentence-measure">00000</span>
      </span>
      <textarea {...props} ref={setField} value={value} rows={1} wrap="soft"
        onCompositionStart={(event) => { composingRef.current = true; onCompositionStart?.(event); }}
        onCompositionEnd={(event) => { composingRef.current = false; onCompositionEnd?.(event); }}
        onKeyDown={(event) => {
          onKeyDown?.(event);
          if (event.defaultPrevented || event.key !== "Enter" || composingRef.current || event.nativeEvent.isComposing || event.keyCode === 229) return;
          event.preventDefault();
          if (onEnter) onEnter();
          else event.currentTarget.form?.requestSubmit();
        }} />
    </span>
  );
});
