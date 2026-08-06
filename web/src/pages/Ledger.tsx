import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { EmptyState, Spinner } from "../components/ui";

export default function Ledger() {
  const { data: ledger } = useQuery({ queryKey: ["ledger"], queryFn: api.ledger });
  const { data: intel } = useQuery({ queryKey: ["intel"], queryFn: () => api.intel({ limit: 100 }) });

  if (!ledger || !intel) return <Spinner label="Caricamento ledger & intel…" />;

  const intelItems = intel.items ?? [];
  const ledgerItems = Array.isArray(ledger) ? ledger : [];

  return (
    <div className="max-w-7xl mx-auto flex flex-col xl:flex-row gap-6">
      <div className="flex-1 flex flex-col gap-4">
        <div className="bg-surface rounded-lg border border-white/5 overflow-hidden">
          <div className="px-5 py-4 border-b border-white/5 bg-[#252d3d]">
            <h2 className="font-headline font-bold text-textPrimary">Ledger — esiti immutabili</h2>
          </div>
          {ledgerItems.length === 0 ? (
            <EmptyState message="Ledger vuoto" />
          ) : (
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="bg-input text-xs font-label text-textSecondary uppercase tracking-wider border-b border-white/5">
                  <th className="px-4 py-3 font-medium">Property</th>
                  <th className="px-4 py-3 font-medium">Outcome</th>
                  <th className="px-4 py-3 font-medium text-right">Probe</th>
                  <th className="px-4 py-3 font-medium text-right">Timestamp</th>
                </tr>
              </thead>
              <tbody className="font-mono text-sm divide-y divide-white/5">
                {ledgerItems.map((l, i) => (
                  <tr key={`${l.property_key}-${l.ts}-${i}`} className={i % 2 === 1 ? "bg-input/30" : ""}>
                    <td className="px-4 py-2.5 text-primary">{l.property_key}</td>
                    <td className="px-4 py-2.5">
                      <span
                        className={`px-2 py-0.5 rounded-full text-[10px] uppercase font-bold tracking-wider border ${
                          l.outcome === "confirmed"
                            ? "bg-tertiary/10 text-tertiary border-tertiary/20"
                            : l.outcome === "denied"
                              ? "bg-error/10 text-error border-error/20"
                              : "bg-secondary/10 text-secondary border-secondary/20"
                        }`}
                      >
                        {l.outcome}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-right text-textSecondary">{l.probe_id ?? "—"}</td>
                    <td className="px-4 py-2.5 text-right text-textSecondary">
                      {new Date(l.ts).toLocaleString("it-IT")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="w-full xl:w-[420px] flex-shrink-0">
        <div className="bg-surface rounded-lg border border-white/5 overflow-hidden flex flex-col h-full">
          <div className="px-5 py-4 border-b border-white/5 bg-[#252d3d]">
            <h2 className="font-headline font-bold text-textPrimary">Intel — leak utili</h2>
          </div>
          <div className="flex-1 overflow-y-auto p-3 space-y-2">
            {intelItems.length === 0 && <div className="text-center font-mono text-xs text-textSecondary py-8">Nessuna intel</div>}
            {intelItems.map((it) => (
              <div key={it.id} className="bg-console rounded p-3 border border-white/5">
                <div className="flex items-center gap-2 mb-1.5">
                  <span className="font-mono text-[10px] text-secondary px-1.5 py-0.5 bg-secondary/10 rounded uppercase">
                    {it.kind}
                  </span>
                  <span className="font-mono text-[10px] text-textSecondary ml-auto">
                    {it.entropy_before.toFixed(1)} → {it.entropy_after.toFixed(1)} bits
                  </span>
                </div>
                <div className="font-mono text-xs text-textPrimary/85 leading-relaxed">“{it.text}”</div>
                {it.note && <div className="font-mono text-[10px] text-tertiary mt-1">{it.note}</div>}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
