import { Outlet, Route, Routes } from "react-router-dom";
import { FooterStrip, Header, LiveClock, Sidebar } from "./components/Layout";
import AttackTree from "./pages/AttackTree";
import Ledger from "./pages/Ledger";
import ProbeLab from "./pages/ProbeLab";
import Properties from "./pages/Properties";
import Review from "./pages/Review";
import Sessions from "./pages/Sessions";
import Status from "./pages/Status";

function Shell() {
  return (
    <div className="h-screen overflow-hidden flex bg-base text-textPrimary font-body">
      <Sidebar target="@HackingA0" />
      <div className="ml-[240px] flex-1 flex flex-col relative">
        <Outlet />
        <FooterStrip counts={{ probes: 0, intel: 0, ledger: 0, frames: 0, memory_entries: 0 }} />
      </div>
    </div>
  );
}

function HeaderSlot({ title, subtitle, status }: { title: string; subtitle: string; status?: string }) {
  return (
    <>
      <Header title={title} subtitle={subtitle} status={status} right={<LiveClock />} />
      <main className="flex-1 overflow-auto p-6 pb-12">
        <Outlet />
      </main>
    </>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route element={<HeaderSlot title="Status" subtitle="Locus / Status" />}>
          <Route index element={<Status />} />
        </Route>
        <Route element={<HeaderSlot title="Proprietà" subtitle="Locus / Proprietà" />}>
          <Route path="properties" element={<Properties />} />
        </Route>
        <Route element={<HeaderSlot title="Probe Lab" subtitle="Locus / Probe Lab" />}>
          <Route path="probe-lab" element={<ProbeLab />} />
        </Route>
        <Route element={<HeaderSlot title="Attack Tree" subtitle="Locus / Attack Tree" />}>
          <Route path="attack-tree" element={<AttackTree />} />
        </Route>
        <Route element={<HeaderSlot title="Review" subtitle="Locus / Review" />}>
          <Route path="review" element={<Review />} />
        </Route>
        <Route element={<HeaderSlot title="Ledger & Intel" subtitle="Locus / Ledger" />}>
          <Route path="ledger" element={<Ledger />} />
        </Route>
        <Route element={<HeaderSlot title="Sessions" subtitle="Locus / Sessions" />}>
          <Route path="sessions" element={<Sessions />} />
        </Route>
      </Route>
    </Routes>
  );
}
