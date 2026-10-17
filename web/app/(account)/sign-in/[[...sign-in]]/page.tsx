import { SignIn } from "@clerk/nextjs";
import Link from "next/link";

export const metadata = { title: "Sign in: Node & Nerve" };

export default function SignInPage() {
  return (
    <main className="auth-page">
      <Link href="/" className="brand auth-brand">
        Node &amp; Nerve
      </Link>
      <SignIn />
    </main>
  );
}
