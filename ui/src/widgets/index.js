// Widget registry — a new life area adds its widget file plus ONE line here.
import AgentMonitor from "./AgentMonitor.jsx";
import Automations from "./Automations.jsx";
import Brief from "./Brief.jsx";
import CommandBox from "./CommandBox.jsx";
import NowPlaying from "./NowPlaying.jsx";
import Stats from "./Stats.jsx";
import Tasks from "./Tasks.jsx";
import Volume from "./Volume.jsx";
import Windows from "./Windows.jsx";

export const widgets = [
  { id: "command", title: "Command", area: null, Component: CommandBox },
  { id: "brief", title: "Today's brief", area: "tasks", Component: Brief },
  { id: "tasks", title: "Tasks", area: "tasks", Component: Tasks },
  { id: "agents", title: "Agent activity", area: null, Component: AgentMonitor },
  { id: "volume", title: "System volume", area: null, Component: Volume },
  { id: "windows", title: "Windows", area: null, Component: Windows },
  { id: "now-playing", title: "Now playing", area: null, Component: NowPlaying },
  { id: "automations", title: "Automations", area: null, Component: Automations },
  { id: "stats", title: "Observability", area: null, Component: Stats },
];
