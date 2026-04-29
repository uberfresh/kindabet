import { BrowserRouter, Routes, Route } from "react-router-dom";
import HomePage from "./pages/HomePage";
import MatchPage from "./pages/MatchPage";
import OpportunitiesPage from "./pages/OpportunitiesPage";
import SettingsPage from "./pages/SettingsPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/"           element={<HomePage />} />
        <Route path="/match/:id"  element={<MatchPage />} />
        <Route path="/firsatlar"  element={<OpportunitiesPage />} />
        <Route path="/ayarlar"    element={<SettingsPage />} />
        <Route path="*"           element={<HomePage />} />
      </Routes>
    </BrowserRouter>
  );
}
