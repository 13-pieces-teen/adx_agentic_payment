import { redirect } from 'next/navigation';

export default function LegacyListingsRedirect() {
  redirect('/game');
}
