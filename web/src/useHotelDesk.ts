import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  api,
  type OperatorApproval,
  type OperatorDecision,
  type OperatorTask,
  type WorldResult,
  type WorldState,
} from "./api";
import { newRequestId } from "./useDemoSession";

/**
 * The hotel's side of the one screen: the manager's review queue, the simulated
 * staff task board and the simulated world. These are the operator routes, never a model
 * tool. The desk re-reads after its own actions and whenever `refreshKey` changes (a guest
 * turn finished, so the agent may have filed a review or a task); nothing else changes the
 * hotel, so it never polls. `afterCommand` runs once a command has committed: a decision,
 * a task move or a world change may have started a hotel update's turn in the guest's
 * conversation, and that turn is shown by re-reading the conversation.
 */
export function useHotelDesk(refreshKey: number, afterCommand?: () => unknown) {
  const [world, setWorld] = useState<WorldState | null>(null);
  const [approvals, setApprovals] = useState<OperatorApproval[]>([]);
  const [tasks, setTasks] = useState<OperatorTask[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const afterCommandRef = useRef(afterCommand);
  afterCommandRef.current = afterCommand;

  const load = useCallback(async () => {
    const mine = ++generation.current;
    try {
      const [state, queue, board] = await Promise.all([
        api.world(),
        api.operatorApprovals("all"),
        api.operatorTasks("open"),
      ]);
      if (mine !== generation.current) return; // a newer read already landed
      setWorld(state);
      setApprovals(queue.approvals);
      setTasks(board.tasks);
      setError(null);
    } catch (err) {
      if (mine === generation.current) {
        setError(err instanceof Error ? err.message : "Could not read the hotel.");
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  /** Run one operator command, then re-read the desk. Errors reach the caller. */
  const command = useCallback(
    async <T,>(run: () => Promise<T>): Promise<T> => {
      setBusy(true);
      try {
        const result = await run();
        // Awaited, so whoever issued the command stays busy until the hotel's turn is shown.
        await afterCommandRef.current?.();
        return result;
      } finally {
        setBusy(false);
        void load();
      }
    },
    [load],
  );

  const decide = useCallback(
    (item: OperatorApproval, decision: "approve" | "reject"): Promise<OperatorDecision> =>
      command(() => api.operatorDecide(item.approval_id, decision, item.version, newRequestId())),
    [command],
  );

  const moveTask = useCallback(
    (task: OperatorTask, action: OperatorTask["actions"][number]) =>
      command(async () => (await api.operatorMoveTask(task, action)).task),
    [command],
  );

  const simulate = useCallback(
    (event: string): Promise<WorldResult> => command(() => api.worldEvent(event, newRequestId())),
    [command],
  );

  const advanceClock = useCallback(
    (minutes: number): Promise<WorldResult> =>
      command(() => api.worldClock(minutes, newRequestId())),
    [command],
  );

  /**
   * Start over from fixture v1. Every guest session dies with the old world. The
   * server refuses (409, retryable) while another request still holds the database open, so
   * a refusal is retried briefly before it reaches the caller.
   */
  const reset = useCallback(
    () =>
      command(async () => {
        for (let attempt = 1; ; attempt += 1) {
          try {
            return await api.worldReset();
          } catch (err) {
            const refused = err instanceof ApiError && err.code === "RESET_REFUSED" && err.retryable;
            if (!refused || attempt >= 3) throw err;
            await new Promise((resolve) => window.setTimeout(resolve, 400 * attempt));
          }
        }
      }),
    [command],
  );

  const pending = approvals.filter((a) => a.status === "pending");

  return {
    world,
    approvals,
    pending,
    tasks,
    error,
    busy,
    refresh: load,
    decide,
    moveTask,
    simulate,
    advanceClock,
    reset,
  };
}

export type HotelDesk = ReturnType<typeof useHotelDesk>;
