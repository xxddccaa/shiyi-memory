import ChatSurface from "./ChatSurface";

export default function AgentChat({ onSource }: { onSource: (id: string) => void }) {
  return <ChatSurface embedded onSource={onSource} />;
}
