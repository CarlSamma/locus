export function KpiCard({ label, value, unit, accent }: { label: string; value: string; unit?: string; accent?: boolean }) {
  return (
    <div className="bg-surface rounded-lg p-4 border border-white/5 relative overflow-hidden group">
      <div className="absolute top-0 right-0 w-16 h-16 bg-primary/5 rounded-bl-full -mr-8 -mt-8 transition-transform group-hover:scale-110" />
      <h3 className="font-label text-xs text-textSecondary uppercase tracking-wider mb-2">{label}</h3>
      <div className={`font-mono text-3xl font-bold ${accent ? "text-primary" : "text-textPrimary"}`}>
        {value}
        {unit && <span className="text-sm font-normal text-primary/60"> {unit}</span>}
      </div>
    </div>
  );
}

export function EntropyBar({
  percent,
  accent = "primary",
  shimmer = true,
}: {
  percent: number;
  accent?: "primary" | "warning";
  shimmer?: boolean;
}) {
  const pct = Math.max(0, Math.min(100, percent));
  return (
    <div className="h-2 w-full bg-input rounded-full overflow-hidden flex">
      <div className={`h-full bg-${accent} relative`} style={{ width: `${pct}%` }}>
        {shimmer && (
          <div
            className="absolute inset-0 bg-white/20 w-full h-full"
            style={{
              backgroundImage: "linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent)",
            }}
          />
        )}
      </div>
    </div>
  );
}

export function StatusDot({ color, label }: { color: string; label: string }) {
  const map: Record<string, string> = {
    primary: "bg-primary",
    warning: "bg-warning",
    tertiary: "bg-tertiary",
    error: "bg-error",
    secondary: "bg-secondary",
  };
  return (
    <span className="flex items-center gap-1.5">
      <span className={`w-1.5 h-1.5 rounded-full ${map[color] ?? "bg-primary"}`} />
      <span className="font-mono text-[10px] uppercase tracking-wider text-textSecondary">{label}</span>
    </span>
  );
}

export function EmptyState({ message }: { message: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-textSecondary">
      <span className="material-symbols-outlined text-4xl text-textSecondary/40">inbox</span>
      <span className="font-mono text-xs">{message}</span>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-12 text-textSecondary">
      <span className="material-symbols-outlined text-2xl animate-spin text-primary">progress_activity</span>
      {label && <span className="font-mono text-xs">{label}</span>}
    </div>
  );
}

export function Panel({ title, children, className = "" }: { title?: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={`bg-surface rounded-lg border border-white/5 overflow-hidden flex flex-col ${className}`}>
      {title && (
        <div className="px-5 py-4 border-b border-white/5 flex justify-between items-center bg-[#252d3d]">
          <h2 className="font-headline font-bold text-textPrimary">{title}</h2>
        </div>
      )}
      {children}
    </div>
  );
}
