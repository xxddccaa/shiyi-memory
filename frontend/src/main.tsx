import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import PetChat from "./components/PetChat";
import { useClientEvents } from "./hooks/useClientEvents";
function Surface() {
  useClientEvents();
  return new URLSearchParams(location.search).get("surface") === "pet" ? (
    <PetChat />
  ) : (
    <App />
  );
}
import "./styles.css";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Surface />
  </React.StrictMode>,
);
