'use client';

/**
 * The composer's own microphone — speaking instead of typing.
 *
 * Entirely separate from the voice engines: words appear in the box as they are
 * said, and they are sent, or edited first, by hand. It never sends anything on
 * its own and never talks to the server.
 *
 * **Only one recognition session runs reliably at a time**, so whoever owns both
 * this and a voice engine has to stop one before starting the other.
 *
 * **The speaking guard is a real backstop, not decoration.** The caller is
 * expected to have silenced the assistant first, but a race — started a moment
 * before playback actually stops, or the assistant beginning to speak for an
 * unrelated reason while this is already open — would otherwise transcribe its
 * own voice straight into the box. The browser's recognition opens its own
 * capture that no echo-cancellation option reaches.
 */
export class Dictation {
  private recognition: SpeechRecognition | null = null;
  private running = false;
  private base = '';
  private settled = '';

  constructor(private readonly hooks: {
    /** The full text as it stands, live. */
    onText?: (text: string) => void;
    onState?: (state: 'idle' | 'listening') => void;
    /** Why it stopped, when it stopped for a reason. Without this it simply
     *  switches itself off and leaves someone talking to a box that is not
     *  listening — the browser's recognition needs a network service it does
     *  not always have. */
    onError?: (message: string) => void;
    isSpeaking?: () => boolean;
  } = {}) {}

  get active(): boolean {
    return this.running;
  }

  get supported(): boolean {
    return typeof window !== 'undefined'
      && Boolean(window.SpeechRecognition ?? window.webkitSpeechRecognition);
  }

  start(baseText = ''): void {
    if (this.running || !this.supported) return;
    if (this.hooks.isSpeaking?.()) return;  // see this class's note
    this.base = baseText ? `${baseText} ` : '';
    this.settled = '';
    this.running = true;
    this.listen();
    this.hooks.onState?.('listening');
  }

  stop(): void {
    if (!this.running) return;
    this.running = false;
    if (this.recognition) {
      this.recognition.onend = null;  // do not auto-restart what we are stopping
      try {
        this.recognition.stop();
      } catch {
        // already stopped
      }
      this.recognition = null;
    }
    // Stopping (unlike aborting) finalises whatever audio was already captured,
    // so one last result arrives AFTER this returns. The guard below is what
    // drops it; clearing here as well means a future gap in that guard cannot
    // resurrect stale text through what survived.
    this.base = '';
    this.settled = '';
    this.hooks.onState?.('idle');
  }

  /**
   * Re-base onto text that was just edited by hand.
   *
   * Setting a field's value in code never fires a real input event — only
   * genuine typing does — so this is only ever reached for an actual edit, and
   * needs no flag to tell the two apart. New speech always lands after wherever
   * the edit left off; an edit made in the MIDDLE does not retroactively get
   * inserted there. A disclosed trade-off, not a hidden one.
   */
  rebase(text: string): void {
    if (!this.running) return;
    const trimmed = String(text || '').replace(/\s+$/, '');
    this.base = trimmed ? `${trimmed} ` : '';
    this.settled = '';
  }

  private listen(): void {
    const Recognition = window.SpeechRecognition ?? window.webkitSpeechRecognition;
    if (!Recognition) return;  // `supported` is checked before this is reached
    const recognition = new Recognition();
    recognition.lang = 'en-US';
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event) => {
      // The trailing result described in `stop` reliably arrives after this is
      // already false. Without this guard it rebuilt the whole utterance and
      // wrote it back into a box the person had just watched clear on send.
      if (!this.running) return;
      if (this.hooks.isSpeaking?.()) return;
      let interim = '';
      let settled = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i]!;
        if (result.isFinal) settled += result[0]!.transcript;
        else interim += result[0]!.transcript;
      }
      if (settled) this.settled += settled;
      this.hooks.onText?.(`${this.base}${this.settled}${interim}`);
    };

    recognition.onerror = (event) => {
      if (event.error === 'no-speech' || event.error === 'aborted') return;  // normal here
      this.hooks.onError?.(
        event.error === 'not-allowed' || event.error === 'service-not-allowed'
          ? 'The microphone was blocked. Allow it in your browser and try again.'
          : event.error === 'network'
            ? 'The browser’s dictation needs an internet connection.'
            : 'Dictation stopped unexpectedly.');
      this.stop();
    };

    recognition.onend = () => {
      // A long continuous session ends on its own; pick it straight back up.
      if (!this.running) return;
      try {
        recognition.start();
      } catch {
        // a start is already pending
      }
    };

    recognition.start();
    this.recognition = recognition;
  }
}
