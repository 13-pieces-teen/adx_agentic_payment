import GameViewer from '@/components/GameViewer';

export default async function GamePage({
  params,
}: {
  params: Promise<{ gameId: string }>;
}) {
  const { gameId } = await params;
  return (
    <div className="site-main">
      <GameViewer gameId={gameId} />
    </div>
  );
}
