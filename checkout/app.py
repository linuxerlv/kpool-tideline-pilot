import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def checkout(state, business_key):
    with closing(sqlite3.connect(state)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS stock (business_key text PRIMARY KEY, available integer NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS orders (business_key text PRIMARY KEY, amount integer NOT NULL)")
            if db.execute("SELECT 1 FROM orders WHERE business_key=?", (business_key,)).fetchone() is None:
                db.execute("INSERT OR IGNORE INTO stock VALUES (?,3)", (business_key,))
                changed = db.execute("UPDATE stock SET available=available-1 WHERE business_key=? AND available>0", (business_key,)).rowcount
                if changed:
                    db.execute("INSERT INTO orders VALUES (?,2590)", (business_key,))
        return (db.execute("SELECT available FROM stock WHERE business_key=?", (business_key,)).fetchone() == (2,)
                and db.execute("SELECT amount FROM orders WHERE business_key=?", (business_key,)).fetchone() == (2590,))


class Check(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/checkout-check":
            self.send_error(404)
            return
        nonce = self.headers.get("X-Kpool-Probe-Nonce", "")
        if not re.fullmatch(r"[0-9a-f]{32}", nonce):
            self.send_error(400)
            return
        outcome = checkout(os.environ["KPOOL_CHECKOUT_DB"], nonce)
        result = {"check": "checkout", "region": "cn-east", "status": "passed" if outcome else "failed",
                  "nonce": nonce, "source_sha": os.environ["KPOOL_SOURCE_SHA"],
                  "artifact": os.environ["KPOOL_ARTIFACT"]}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        if not checkout(":memory:", "self-test"):
            raise SystemExit("checkout transaction failed")
        print("checkout test passed: order amount and stock update committed")
    else:
        ThreadingHTTPServer(("0.0.0.0", 8080), Check).serve_forever()
