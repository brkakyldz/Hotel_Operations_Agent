import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  NetworkError,
  TERMINAL,
  api,
  type RequestItem,
  type ReservationView,
  type RunView,
  type SessionInfo,
} from "./api";

/**
 * The browser keeps one session per guest: switching guests, reloading or opening another tab
 * never loses a conversation. Local storage, not per-tab storage: every running copy has one
 * user, and the hotel tells a guest things in their newest conversation.
 */
const STORAGE_KEY = "hotel-demo-sessions";
/** Said when the server no longer knows a stored conversation (a start over removed it). */
export const CLOSED_NOTICE =
  "That conversation is no longer open: the hotel was started over. Pick the guest again.";
const POLL_MS = 1000;
/** Receipts whose request can still move on (a review decided, a task finished). */
const OPEN_STATUSES = new Set(["pending", "in_progress"]);
const MAX_BACKOFF_MS = 8000;
const RESTORE_ATTEMPTS = 6;

export interface Turn {
  clientRequestId: string;
  message: string;
  runId: string | null;
  run: RunView | null;
  /** Error before admission (never accepted) or while polling. */
  error: { code: string; message: string } | null;
  phase: "submitting" | "accepted" | "done" | "rejected";
  /** Provisional streamed reply while the run works; the persisted reply replaces it. */
  liveText?: string | null;
}

interface Stored {
  /** The guest whose conversation is on screen. */
  active: string | null;
  /** guestId → that guest's own session capability (each token is bound to one guest). */
  tokens: Record<string, string>;
}

function readStored(): Stored {
  const empty: Stored = { active: null, tokens: {} };
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Stored>;
      return {
        active: typeof parsed.active === "string" ? parsed.active : null,
        tokens: parsed.tokens && typeof parsed.tokens === "object" ? { ...parsed.tokens } : {},
      };
    }
  } catch {
    // Unreadable or unavailable storage: start without stored sessions.
  }
  return empty;
}

/** After a start-over every stored guest capability belongs to a discarded world. */
export function forgetStoredSessions(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Storage unavailable: nothing was stored.
  }
}

function writeStored(value: Stored): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  } catch {
    // Storage unavailable (private mode); sessions simply are not restorable on reload.
  }
}

function updateStored(change: (s: Stored) => void): void {
  const current = readStored();
  change(current);
  writeStored(current);
}

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(t);
      resolve();
    });
  });

export function newRequestId(): string {
  return crypto.randomUUID();
}

function isTransient(err: unknown): boolean {
  return err instanceof NetworkError || (err instanceof ApiError && err.status >= 500);
}

/** Newest first, no duplicates: a refreshed first page plus any older pages already shown. */
function mergeRequests(firstPage: RequestItem[], shown: RequestItem[]): RequestItem[] {
  const ids = new Set(firstPage.map((r) => r.id));
  return [...firstPage, ...shown.filter((r) => !ids.has(r.id))];
}

/** A run the server already has, shown as a turn (a reload, or a turn the hotel started). */
function runTurn(run: RunView): Turn {
  return {
    clientRequestId: run.client_request_id,
    message: run.user_message,
    runId: run.run_id,
    run,
    error: null,
    phase: TERMINAL.has(run.status) ? "done" : "accepted",
  };
}

/** What became of a message the guest sent. */
export type SendResult = "accepted" | "busy" | "failed";

/** A stored session that could not be restored after retries (network/5xx), kept for Retry. */
export interface RestoreProblem {
  guestId: string;
  message: string;
}

export function useDemoSession() {
  const [token, setToken] = useState<string | null>(null);
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const [requests, setRequests] = useState<RequestItem[]>([]);
  const [olderCursor, setOlderCursor] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [reservation, setReservation] = useState<ReservationView | null>(null);
  const [restoreProblem, setRestoreProblem] = useState<RestoreProblem | null>(null);
  const tokenRef = useRef<string | null>(null);
  const guestRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController>(new AbortController());
  // Runs and request keys already on screen, kept outside React state so a refresh that
  // runs before the next render still knows them (a run is never shown twice).
  const seenRef = useRef({ runs: new Set<string>(), keys: new Set<string>() });
  // Set once every older page of requests has been read, so a refresh of the first page does
  // not offer "Show older requests" again.
  const olderDoneRef = useRef(false);
  // Finished runs whose receipt can still move on, kept outside React state for the same
  // reason: the refresh that follows a finished turn may run before the next render.
  const openReceiptsRef = useRef(new Set<string>());
  const noteRun = useCallback((run: RunView) => {
    const open =
      TERMINAL.has(run.status) &&
      run.artifacts.some((a) => OPEN_STATUSES.has(a.current_status ?? ""));
    if (open) openReceiptsRef.current.add(run.run_id);
    else openReceiptsRef.current.delete(run.run_id);
  }, []);
  // Set once `follow` exists: a refresh that finds a turn the hotel started watches it too.
  const followRef = useRef<
    ((tok: string, clientRequestId: string, runId: string, signal: AbortSignal) => Promise<void>) | null
  >(null);

  const patchTurn = useCallback((clientRequestId: string, patch: Partial<Turn>) => {
    if (patch.run) noteRun(patch.run);
    setTurns((prev) =>
      prev.map((t) => (t.clientRequestId === clientRequestId ? { ...t, ...patch } : t)),
    );
  }, [noteRun]);

  /** Drop what is on screen (not the stored sessions) and stop this view's polling. */
  const clearView = useCallback(() => {
    seenRef.current = { runs: new Set(), keys: new Set() };
    openReceiptsRef.current = new Set();
    olderDoneRef.current = false;
    abortRef.current.abort();
    abortRef.current = new AbortController();
    tokenRef.current = null;
    guestRef.current = null;
    setToken(null);
    setSession(null);
    setTurns([]);
    setRequests([]);
    setOlderCursor(null);
    setReservation(null);
  }, []);

  /**
   * The server rejected this guest's session (closed, invalid or rebound): forget it. `tok` is
   * the session the rejected request used; a late answer for an earlier guest's session must
   * not close the conversation now on screen.
   */
  const expire = useCallback(
    (message: string, tok: string) => {
      if (tokenRef.current !== tok) return;
      const guestId = guestRef.current;
      updateStored((s) => {
        if (guestId) delete s.tokens[guestId];
        s.active = null;
      });
      clearView();
      setNotice(message);
    },
    [clearView],
  );

  const refreshSession = useCallback(
    async (tok: string) => {
      try {
        const [info, reqs, current] = await Promise.all([
          api.session(tok),
          api.requests(tok),
          api.reservation(tok),
        ]);
        // Ignore a late response for a guest that is no longer on screen.
        if (tokenRef.current !== tok) return;
        setSession(info);
        setRequests((shown) => mergeRequests(reqs.requests, shown));
        setOlderCursor((cursor) => cursor ?? (olderDoneRef.current ? null : reqs.next_cursor));
        setReservation(current.reservation);
        // A receipt shown as pending may have moved on (a review expired or was decided, a
        // task finished): re-read those finished turns so the receipt and the hotel's
        // message about it never contradict each other on screen.
        const open = [...openReceiptsRef.current];
        if (open.length > 0) {
          const reread = await Promise.all(open.map((id) => api.run(tok, id)));
          if (tokenRef.current !== tok) return;
          reread.forEach(noteRun);
          const byId = new Map(reread.map((r) => [r.run_id, r]));
          setTurns((prev) =>
            prev.map((t) => (t.runId && byId.has(t.runId) ? { ...t, run: byId.get(t.runId)! } : t)),
          );
        }
        // A turn the hotel started appears in the session's runs without a message
        // from this tab: show it and watch it like any other. A turn this tab is still
        // submitting is recognised by its request key, so it is never shown twice.
        const seen = seenRef.current;
        const fresh = info.run_ids.filter((id) => !seen.runs.has(id));
        if (fresh.length === 0) return;
        fresh.forEach((id) => seen.runs.add(id));
        let fetched: RunView[];
        try {
          fetched = await Promise.all(fresh.map((id) => api.run(tok, id)));
        } catch (err) {
          fresh.forEach((id) => seen.runs.delete(id)); // try again on the next refresh
          throw err;
        }
        const runs = fetched.filter((run) => !seen.keys.has(run.client_request_id));
        if (tokenRef.current !== tok || runs.length === 0) return;
        runs.forEach((run) => {
          seen.keys.add(run.client_request_id);
          noteRun(run);
        });
        setTurns((prev) => [...prev, ...runs.map(runTurn)]);
        const signal = abortRef.current.signal;
        for (const run of runs) {
          if (!TERMINAL.has(run.status)) {
            void followRef.current?.(tok, run.client_request_id, run.run_id, signal);
          }
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) expire(CLOSED_NOTICE, tok);
        else if (err instanceof ApiError && err.status === 409) expire(err.message, tok);
      }
    },
    [expire, noteRun],
  );

  const poll = useCallback(
    async (tok: string, clientRequestId: string, runId: string, signal: AbortSignal) => {
      let backoff = POLL_MS;
      while (!signal.aborted) {
        try {
          const run = await api.run(tok, runId);
          if (signal.aborted) return;
          patchTurn(clientRequestId, { run, runId, error: null });
          if (TERMINAL.has(run.status)) {
            patchTurn(clientRequestId, { phase: "done", liveText: null });
            void refreshSession(tok);
            return;
          }
          backoff = POLL_MS;
        } catch (err) {
          if (signal.aborted) return;
          if (err instanceof ApiError && err.status === 401) {
            expire(CLOSED_NOTICE, tok);
            return;
          }
          if (err instanceof ApiError && err.status === 404) {
            patchTurn(clientRequestId, {
              phase: "done",
              error: { code: err.code, message: "This request is no longer available." },
            });
            return;
          }
          // Transient network trouble: back off, keep polling the same run (never re-submit).
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
          patchTurn(clientRequestId, {
            error: { code: "NETWORK", message: "Connection problem — still checking…" },
          });
        }
        await sleep(backoff, signal);
      }
    },
    [expire, patchTurn, refreshSession],
  );

  /**
   * Watch a run: stream its provisional text and live tool steps, then read the
   * persisted run, which is authoritative. When streaming is unavailable or breaks, the same
   * polling as before takes over; nothing is ever re-submitted.
   */
  const follow = useCallback(
    async (tok: string, clientRequestId: string, runId: string, signal: AbortSignal) => {
      let seenActivity = 0;
      await api.streamRun(
        tok,
        runId,
        (snapshot) => {
          if (signal.aborted) return;
          patchTurn(clientRequestId, { liveText: snapshot.text || null });
          if (snapshot.activity !== seenActivity) {
            seenActivity = snapshot.activity;
            api
              .run(tok, runId)
              .then((run) => {
                if (signal.aborted || TERMINAL.has(run.status)) return;
                // Never let a late in-progress read overwrite a finished turn.
                setTurns((prev) =>
                  prev.map((t) =>
                    t.clientRequestId === clientRequestId && t.phase !== "done" ? { ...t, run } : t,
                  ),
                );
              })
              .catch(() => {});
          }
        },
        signal,
      );
      if (!signal.aborted) await poll(tok, clientRequestId, runId, signal);
    },
    [patchTurn, poll],
  );
  followRef.current = follow;

  /**
   * Show an existing session: its runs, requests and reservation come from the server.
   * Throws on failure without touching the screen, so the caller decides what it means.
   */
  const loadSession = useCallback(
    async (guestId: string, tok: string, signal: AbortSignal) => {
      const info = await api.session(tok);
      const [runs, reqs, current] = await Promise.all([
        Promise.all(info.run_ids.map((id) => api.run(tok, id))),
        api.requests(tok),
        api.reservation(tok),
      ]);
      if (signal.aborted) return;
      tokenRef.current = tok;
      guestRef.current = guestId;
      setToken(tok);
      setSession(info);
      setRequests(reqs.requests);
      setOlderCursor(reqs.next_cursor);
      olderDoneRef.current = false;
      setReservation(current.reservation);
      seenRef.current = {
        runs: new Set(runs.map((r) => r.run_id)),
        keys: new Set(runs.map((r) => r.client_request_id)),
      };
      runs.forEach(noteRun);
      setTurns(runs.map(runTurn));
      for (const run of runs) {
        if (!TERMINAL.has(run.status)) {
          void follow(tok, run.client_request_id, run.run_id, signal);
        }
      }
    },
    [follow, noteRun],
  );

  const startSession = useCallback(
    async (guestId: string, signal: AbortSignal) => {
      const created = await api.createSession(guestId);
      if (signal.aborted) return;
      updateStored((s) => {
        s.tokens[guestId] = created.token;
        s.active = guestId;
      });
      tokenRef.current = created.token;
      guestRef.current = guestId;
      setToken(created.token);
      await refreshSession(created.token);
    },
    [refreshSession],
  );

  /**
   * Restore a guest's stored session, retrying transient failures (network, or a 5xx while
   * the backend restarts). A rejected session is forgotten and, if `fallbackToNew`, replaced.
   * Returns false when retries ran out: the stored token is kept for a manual Retry.
   */
  const restoreOrStart = useCallback(
    async (guestId: string, signal: AbortSignal, fallbackToNew: boolean): Promise<boolean> => {
      const stored = readStored().tokens[guestId];
      if (!stored) {
        if (fallbackToNew) await startSession(guestId, signal);
        return true;
      }
      let backoff = POLL_MS;
      for (let attempt = 1; !signal.aborted; attempt += 1) {
        try {
          await loadSession(guestId, stored, signal);
          updateStored((s) => {
            s.active = guestId;
          });
          return true;
        } catch (err) {
          if (!isTransient(err)) {
            updateStored((s) => {
              if (s.tokens[guestId] === stored) delete s.tokens[guestId];
              if (s.active === guestId) s.active = null;
            });
            if (fallbackToNew) {
              await startSession(guestId, signal);
            } else if (err instanceof ApiError) {
              setNotice(err.status === 401 ? CLOSED_NOTICE : err.message);
            }
            return true;
          }
          if (attempt >= RESTORE_ATTEMPTS) return false;
          await sleep(backoff, signal);
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        }
      }
      return true;
    },
    [loadSession, startSession],
  );

  const reportRestoreProblem = useCallback((guestId: string) => {
    setRestoreProblem({
      guestId,
      message:
        "Could not reach the demo server to restore this conversation. It is kept in this tab; " +
        "retry when the backend is running.",
    });
  }, []);

  // Restore the active guest's session after a reload.
  useEffect(() => {
    const controller = new AbortController();
    abortRef.current = controller;
    const active = readStored().active;
    if (!active) return () => controller.abort();
    guestRef.current = active;
    setSwitching(true);
    void (async () => {
      try {
        const ok = await restoreOrStart(active, controller.signal, false);
        if (!ok && !controller.signal.aborted) reportRestoreProblem(active);
      } catch (err) {
        if (!controller.signal.aborted) setNotice(err instanceof Error ? err.message : null);
      } finally {
        if (!controller.signal.aborted) setSwitching(false);
      }
    })();
    return () => controller.abort();
  }, [reportRestoreProblem, restoreOrStart]);

  /** Show a guest's conversation: their existing session if still valid, else a new one. */
  const selectGuest = useCallback(
    async (guestId: string) => {
      if (guestId === guestRef.current && tokenRef.current) return; // already on screen
      clearView();
      guestRef.current = guestId;
      const signal = abortRef.current.signal;
      setSwitching(true);
      setNotice(null);
      setRestoreProblem(null);
      try {
        const ok = await restoreOrStart(guestId, signal, true);
        if (!ok && !signal.aborted) reportRestoreProblem(guestId);
      } catch (err) {
        if (!signal.aborted) {
          setNotice(err instanceof Error ? err.message : "Could not start a session.");
        }
      } finally {
        if (!signal.aborted) setSwitching(false);
      }
    },
    [clearView, reportRestoreProblem, restoreOrStart],
  );

  /**
   * Show a fresh conversation for a guest (the one on screen by default); their records
   * stay, the chat is new. The guided scenarios start each run this way.
   */
  const startConversation = useCallback(async (target?: string) => {
    const guestId = target ?? guestRef.current;
    if (!guestId) return;
    clearView();
    guestRef.current = guestId;
    const signal = abortRef.current.signal;
    setSwitching(true);
    setNotice(null);
    setRestoreProblem(null);
    try {
      await startSession(guestId, signal);
    } catch (err) {
      if (!signal.aborted) {
        setNotice(err instanceof Error ? err.message : "Could not start a session.");
      }
    } finally {
      if (!signal.aborted) setSwitching(false);
    }
  }, [clearView, startSession]);

  const newConversation = useCallback(() => startConversation(), [startConversation]);

  /** Take the conversation off screen (before a start-over discards the whole hotel). */
  const leave = useCallback(() => {
    clearView(); // aborts a conversation still opening, whose own cleanup then never runs
    setSwitching(false);
    setNotice(null);
    setRestoreProblem(null);
  }, [clearView]);

  const retryRestore = useCallback(async () => {
    const problem = restoreProblem;
    if (!problem) return;
    setRestoreProblem(null);
    clearView();
    guestRef.current = problem.guestId;
    const signal = abortRef.current.signal;
    setSwitching(true);
    try {
      const ok = await restoreOrStart(problem.guestId, signal, false);
      if (!ok && !signal.aborted) reportRestoreProblem(problem.guestId);
    } finally {
      if (!signal.aborted) setSwitching(false);
    }
  }, [clearView, reportRestoreProblem, restoreOrStart, restoreProblem]);

  const send = useCallback(
    async (message: string): Promise<SendResult> => {
      if (!token) return "failed";
      const clientRequestId = newRequestId();
      const signal = abortRef.current.signal;
      seenRef.current.keys.add(clientRequestId);
      setTurns((prev) => [
        ...prev,
        { clientRequestId, message, runId: null, run: null, error: null, phase: "submitting" },
      ]);
      // A lost HTTP response is retried with the SAME request key, so the server replays
      // the existing run instead of executing the intent twice.
      let attempt = 0;
      while (!signal.aborted) {
        try {
          const accepted = await api.chat(token, clientRequestId, message);
          seenRef.current.runs.add(accepted.run_id);
          patchTurn(clientRequestId, { runId: accepted.run_id, phase: "accepted", error: null });
          await follow(token, clientRequestId, accepted.run_id, signal);
          return "accepted";
        } catch (err) {
          if (err instanceof NetworkError && attempt < 3) {
            attempt += 1;
            await sleep(500 * 2 ** attempt, signal);
            continue;
          }
          if (err instanceof ApiError && err.status === 401) {
            expire(CLOSED_NOTICE, token);
            return "failed";
          }
          if (err instanceof ApiError && err.code === "RUN_IN_PROGRESS") {
            // The hotel started a turn in this conversation a moment ago: the
            // message was not taken. Show that turn instead; the caller keeps the draft.
            seenRef.current.keys.delete(clientRequestId);
            setTurns((prev) => prev.filter((t) => t.clientRequestId !== clientRequestId));
            void refreshSession(token);
            return "busy";
          }
          const e =
            err instanceof ApiError
              ? { code: err.code, message: err.message }
              : { code: "NETWORK", message: "Could not reach the demo server." };
          patchTurn(clientRequestId, { phase: "rejected", error: e });
          return "failed";
        }
      }
      return "failed";
    },
    [expire, follow, patchTurn, refreshSession, token],
  );

  const active = turns.some((t) => t.phase === "submitting" || t.phase === "accepted");

  /** Re-read the conversation; resolves once any turn the hotel started is on screen. */
  const refresh = useCallback(async () => {
    if (tokenRef.current) await refreshSession(tokenRef.current);
  }, [refreshSession]);

  const loadOlder = useCallback(async () => {
    const tok = tokenRef.current;
    if (!tok || !olderCursor) return;
    setLoadingOlder(true);
    try {
      const page = await api.requests(tok, olderCursor);
      if (tokenRef.current !== tok) return;
      setRequests((shown) => {
        const ids = new Set(shown.map((r) => r.id));
        return [...shown, ...page.requests.filter((r) => !ids.has(r.id))];
      });
      setOlderCursor(page.next_cursor);
      if (page.next_cursor === null) olderDoneRef.current = true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) expire(CLOSED_NOTICE, tok);
      else if (err instanceof ApiError && err.status === 409) expire(err.message, tok);
    } finally {
      setLoadingOlder(false);
    }
  }, [expire, olderCursor]);

  return {
    refresh,
    token,
    session,
    turns,
    requests,
    hasOlderRequests: olderCursor !== null,
    loadingOlder,
    loadOlder,
    reservation,
    notice,
    restoreProblem,
    retryRestore,
    switching,
    active,
    selectGuest,
    newConversation,
    startConversation,
    leave,
    send,
  };
}
