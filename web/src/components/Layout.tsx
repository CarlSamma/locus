import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

const NAV = [
  { to: "/", label: "Status", icon: "dashboard", end: true },
  { to: "/properties", label: "Proprietà", icon: "analytics" },
  { to: "/probe-lab", label: "Probe Lab", icon: "science" },
  { to: "/attack-tree", label: "Attack Tree", icon: "account_tree" },
  { to: "/review", label: "Review", icon: "rate_review" },
  { to: "/ledger", label: "Ledger & Intel", icon: "menu_book" },
  { to: "/sessions", label: "Sessions", icon: "history" },
];

export function Sidebar({ target }: { target: string }) {
  return (
    <aside className="w-[240px] h-screen fixed left-0 top-0 bg-[#161a27] border-r border-white/10 flex flex-col py-6 px-4 z-50">
      <div className="mb-8 px-2 flex items-center gap-3">
        <div className="w-8 h-8 rounded bg-primary/10 flex items-center justify-center border border-primary/30 shadow-[0_0_10px_rgba(79,209,197,0.2)]">
          <span className="material-symbols-outlined text-primary text-xl">radar</span>
        </div>
        <div>
          <div className="font-headline font-bold text-primary tracking-tight leading-none text-xl">LOCUS</div>
          <div className="font-mono text-[10px] text-textSecondary mt-1 uppercase tracking-wider">{target}</div>
        </div>
      </div>
      <nav className="flex-1 space-y-1">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2.5 rounded-lg font-body text-sm transition-colors ${
                isActive
                  ? "bg-white/5 text-primary border-b-2 border-primary font-bold"
                  : "text-textSecondary hover:text-primary hover:bg-white/5"
              }`
            }
          >
            <span className="material-symbols-outlined text-[20px]">{item.icon}</span>
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="mt-auto pt-6 border-t border-white/10">
        <NavLink
          to="/probe-lab"
          className="w-full flex items-center justify-center gap-2 bg-primary text-console py-2 rounded-lg font-headline font-bold text-sm hover:shadow-[0_0_15px_rgba(79,209,197,0.4)] transition-all"
        >
          <span className="material-symbols-outlined text-lg">add_circle</span>
          New Probe
        </NavLink>
      </div>
    </aside>
  );
}

export function Header({
  title,
  subtitle,
  status,
  right,
}: {
  title: string;
  subtitle?: string;
  status?: string;
  right?: ReactNode;
}) {
  return (
    <header className="sticky top-0 w-full h-[64px] z-40 bg-base/80 backdrop-blur-md border-b border-white/10 flex justify-between items-center px-6">
      <div className="flex items-center gap-4">
        <h1 className="font-headline text-lg font-bold text-textPrimary">{title}</h1>
        {subtitle && (
          <>
            <div className="h-4 w-px bg-white/10" />
            <div className="font-mono text-xs text-textSecondary">{subtitle}</div>
          </>
        )}
      </div>
      <div className="flex items-center gap-6">
        {status && (
          <div className="flex items-center gap-2 px-3 py-1 rounded-full bg-warning/10 border border-warning/30 text-warning font-mono text-[10px] uppercase font-bold tracking-wider animate-pulse">
            <div className="w-1.5 h-1.5 rounded-full bg-warning" />
            {status}
          </div>
        )}
        {right}
      </div>
    </header>
  );
}

export function StatusChip({ state }: { state: string }) {
  const style: Record<string, string> = {
    confirmed: "bg-tertiary/10 text-tertiary border-tertiary/20",
    denied: "bg-error/10 text-error border-error/20",
    running: "bg-warning/10 text-warning border-warning/20",
    replied: "bg-secondary/10 text-secondary border-secondary/20",
    unknown: "bg-white/5 text-textSecondary border-white/10",
  };
  const cls = style[state] ?? style.unknown;
  return (
    <span
      className={`px-2 py-0.5 rounded-full border text-[10px] uppercase font-bold tracking-wider font-mono ${cls}`}
    >
      {state}
    </span>
  );
}

export function FooterStrip({ counts }: { counts: Record<string, number> }) {
  const items: Array<[string, number]> = [
    ["PROBES", counts["probes"] ?? 0],
    ["INTEL", counts["intel"] ?? 0],
    ["LEDGER", counts["ledger"] ?? 0],
    ["FRAMES", counts["frames"] ?? 0],
    ["MEMORY", counts["memory_entries"] ?? 0],
  ];
  return (
    <footer className="fixed bottom-0 right-0 left-[240px] h-[32px] bg-console border-t border-white/10 flex items-center px-4 z-40">
      <div className="flex gap-6 font-mono text-[10px] text-textSecondary tracking-wider">
        {items.map(([label, value], i) => (
          <div key={label} className="flex items-center gap-1.5">
            {i > 0 && <div className="w-px h-3 bg-white/10 mr-5" />}
            <span className="text-white/30">{label}:</span>
            <span className="text-primary font-bold">{value}</span>
          </div>
        ))}
      </div>
      <div className="ml-auto flex items-center gap-2 font-mono text-[10px]">
        <span className="w-2 h-2 rounded-full bg-tertiary animate-pulse shadow-[0_0_8px_rgba(154,230,180,0.6)]" />
        <span className="text-tertiary">SYSTEM ONLINE</span>
      </div>
    </footer>
  );
}

export function LiveClock() {
  return (
    <div className="font-mono text-sm text-primary flex items-center gap-2 bg-primary/5 px-3 py-1.5 rounded border border-primary/20">
      <span className="material-symbols-outlined text-[16px]">schedule</span>
      <span>{new Date().toLocaleTimeString("it-IT", { hour12: false })}</span>
    </div>
  );
}
