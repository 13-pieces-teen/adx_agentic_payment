import { redirect } from 'next/navigation';

export default function LegacyArenaRedirect() {
  redirect('/game');
}
