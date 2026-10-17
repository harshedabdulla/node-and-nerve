import { SignUp } from "@clerk/nextjs";
import Link from "next/link";

export const metadata = { title: "Create an account: Node & Nerve" };

export default function SignUpPage() {
  return (
    <main className="auth-page">
      <Link href="/" className="brand auth-brand">
        Node &amp; Nerve
      </Link>
      <SignUp />
    </main>
  );
}
