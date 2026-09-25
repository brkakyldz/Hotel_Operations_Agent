// Offline E2E ports. Override with HOTEL_E2E_API_PORT / HOTEL_E2E_UI_PORT so several
// checkouts (e.g. parallel worktrees) can run their journeys at the same time.
export const API_PORT = Number(process.env.HOTEL_E2E_API_PORT ?? 8765);
export const UI_PORT = Number(process.env.HOTEL_E2E_UI_PORT ?? 5174);
export const API = `http://127.0.0.1:${API_PORT}`;
