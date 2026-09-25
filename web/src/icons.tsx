// Small inline line icons (decorative: always aria-hidden). No icon font or network fetch.
import type { ReactNode } from "react";

function Svg({ children, size = 20 }: { children: ReactNode; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export const WrenchIcon = () => (
  <Svg>
    <path d="M14.7 6.3a4 4 0 0 0-5.4 5.1L3.5 17.2a1.8 1.8 0 0 0 2.5 2.5l5.8-5.8a4 4 0 0 0 5.1-5.4l-2.6 2.6-2.3-.6-.6-2.3z" />
  </Svg>
);

export const TowelsIcon = () => (
  <Svg>
    <rect x="4" y="5" width="16" height="4" rx="2" />
    <rect x="4" y="10" width="16" height="4" rx="2" />
    <rect x="4" y="15" width="16" height="4" rx="2" />
  </Svg>
);

export const ClockIcon = () => (
  <Svg>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 2" />
  </Svg>
);

export const DoorIcon = () => (
  <Svg>
    <path d="M6 20V4.8A.8.8 0 0 1 6.8 4h10.4a.8.8 0 0 1 .8.8V20" />
    <path d="M4 20h16" />
    <circle cx="14.5" cy="12.5" r=".6" fill="currentColor" />
  </Svg>
);

export const SendIcon = () => (
  <Svg size={22}>
    <path d="M12 19V5" />
    <path d="M6 11l6-6 6 6" />
  </Svg>
);

export const RefreshIcon = () => (
  <Svg size={14}>
    <path d="M19 12a7 7 0 1 1-2.1-5" />
    <path d="M19 4.5V8h-3.5" />
  </Svg>
);

/** The agent's mark: a concierge's desk bell whose knob is a small spark. */
export const AgentMark = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <g fill="currentColor">
      <path d="M12 1.8c.3 2.3.9 2.9 3.2 3.2-2.3.3-2.9.9-3.2 3.2-.3-2.3-.9-2.9-3.2-3.2 2.3-.3 2.9-.9 3.2-3.2z" />
      <path d="M4.6 17.2a7.4 7.4 0 0 1 14.8 0z" />
      <rect x="3" y="18.6" width="18" height="2.1" rx="1.05" />
    </g>
  </svg>
);

export const BellIcon = () => (
  <Svg size={18}>
    <path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15z" />
    <path d="M10 20.5a2 2 0 0 0 4 0" />
  </Svg>
);
