import { useState } from "react";
import { Icon } from "./Navigation";
import type { Settings, WordList } from "./types";

const KEY = "cvt.onboarding.v1.";

// Only a fresh registration on this device asks for the guide. Existing
// accounts signing in elsewhere go straight to their overview.
export function requestOnboarding(userId: number) {
  try { localStorage.setItem(KEY + userId, "1"); } catch { /* The guide is optional without storage. */ }
}

export function onboardingPending(userId: number): boolean {
  try { return localStorage.getItem(KEY + userId) === "1"; } catch { return false; }
}

function finishOnboarding(userId: number) {
  try { localStorage.removeItem(KEY + userId); } catch { /* Nothing to clear. */ }
}

const STEPS = 4;

export function Onboarding({ userId, wordLists, wordListsReady, settings, saving, onToggleList, onSetting, onFinish }: {
  userId: number; wordLists: WordList[]; wordListsReady: boolean; settings: Settings; saving: boolean;
  onToggleList: (listId: string, selected: boolean) => void;
  onSetting: (next: Partial<Settings>) => void;
  onFinish: (next: "study" | "assessment" | "home") => void;
}) {
  const [step, setStep] = useState(0);
  const selected = settings.selected_word_list_ids;
  function finish(next: "study" | "assessment" | "home") {
    finishOnboarding(userId);
    onFinish(next);
  }
  return <div className="onboarding" role="dialog" aria-modal="true" aria-label="新手引导">
    <header className="onboarding-header">
      {step > 0 ? <button type="button" className="icon-button" aria-label="上一步" onClick={() => setStep(step - 1)}><Icon name="back" /></button>
        : <span className="header-spacer" />}
      <div className="onboarding-dots" role="img" aria-label={`第 ${step + 1} 步，共 ${STEPS} 步`}>
        {Array.from({ length: STEPS }, (_, index) => <span key={index} className={index === step ? "is-active" : ""} />)}
      </div>
      <button type="button" className="text-button" onClick={() => finish("home")}>跳过</button>
    </header>

    <div className="onboarding-body">
      {step === 0 ? <>
        <h1>在句子里记单词</h1>
        <div className="question-card onboarding-demo" aria-hidden="true">
          <p className="english-sentence">She decided to <span className="onboarding-blank">abandon</span> the plan.</p>
          <div className="meaning-block"><p className="word-meaning">抛弃；放弃</p></div>
        </div>
        <p className="onboarding-text">每道题是一句英文和一个中文释义，把空格里的单词填出来。之后系统按你的作答安排复习。</p>
      </> : null}

      {step === 1 ? <>
        <h1>这样作答</h1>
        <ul className="onboarding-points">
          <li><span><Icon name="arrow" /></span><p><strong>输入后按回车</strong>答对会朗读整句并进入下一题</p></li>
          <li><span><Icon name="check" /></span><p><strong>想不起来就直接回车</strong>看到答案后照着填一遍</p></li>
          <li><span><Icon name="sound" /></span><p><strong>点喇叭听发音</strong>听过提示的作答不算独立答对</p></li>
          <li><span><Icon name="back" /></span><p><strong>向右滑动卡片</strong>回看上一题，向左滑回来</p></li>
        </ul>
      </> : null}

      {step === 2 ? <>
        <h1>选择词库</h1>
        <p className="onboarding-text">可多选，之后能在设置里调整。</p>
        <div className="panel onboarding-lists">
          {!wordListsReady ? <p className="settings-hint" role="status">加载中…</p> : wordLists.map((list) => (
            <label className="setting-row" key={list.list_id}>
              <span><strong>{list.title}</strong><small>{list.word_count.toLocaleString()} 词</small></span>
              <input type="checkbox" className="setting-switch" disabled={saving} checked={selected.includes(list.list_id)}
                onChange={(event) => onToggleList(list.list_id, event.target.checked)} />
            </label>
          ))}
        </div>
      </> : null}

      {step === 3 ? <>
        <h1>从哪里开始</h1>
        <div className="panel onboarding-lists">
          <label className="setting-row">
            <span><strong>跳过基础 800 词</strong><small>the、of、and 这类最常见的词不再出题</small></span>
            <input type="checkbox" className="setting-switch" disabled={saving} checked={settings.skip_basic_600}
              onChange={(event) => onSetting({ skip_basic_600: event.target.checked })} />
          </label>
        </div>
        <p className="onboarding-text">做一次 5–6 分钟的词汇水平测验，可以跳过你已经掌握的范围。</p>
      </> : null}
    </div>

    <footer className="onboarding-actions">
      {step < STEPS - 1 ? <button type="button" className="primary-button"
        disabled={step === 2 && (saving || selected.length === 0)} onClick={() => setStep(step + 1)}>下一步</button> : <>
        <button type="button" className="primary-button" disabled={saving} onClick={() => finish("study")}>开始学习</button>
        <button type="button" className="secondary-button" disabled={saving} onClick={() => finish("assessment")}>先测词汇水平</button>
      </>}
    </footer>
  </div>;
}
