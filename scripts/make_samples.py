"""Build the bundled sample databases deterministically.

Run:  python scripts/make_samples.py [--only university|retail|nokeys|dump]
"""

from __future__ import annotations

import argparse
import random
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

SEED = 20251021

FIRST = ["Aarav", "Diya", "Ishaan", "Meera", "Kabir", "Ananya", "Rohan", "Sara",
         "Vivaan", "Nisha", "Arjun", "Priya", "Dev", "Tara", "Aditya", "Riya",
         "Karan", "Isha", "Nikhil", "Pooja", "Siddharth", "Lakshmi", "Varun",
         "Anjali", "Manav", "Shreya", "Yash", "Neha", "Rahul", "Divya"]
LAST = ["Sharma", "Patel", "Reddy", "Nair", "Iyer", "Gupta", "Mehta", "Rao",
        "Singh", "Desai", "Kulkarni", "Banerjee", "Chopra", "Joshi", "Menon"]
CITIES = ["Hyderabad", "Bengaluru", "Chennai", "Mumbai", "Delhi", "Pune",
          "Kolkata", "Ahmedabad", "Jaipur", "Kochi"]
COUNTRIES = ["India", "Singapore", "Germany", "Canada", "Australia"]
BUILDINGS = ["Block A", "Block B", "Block C", "Annex", "Main Hall"]
DESIGNATIONS = ["Professor", "Associate Professor", "Assistant Professor", "Lecturer"]
DEPARTMENTS = [
    ("CSE", "Block A", 5200000.0),
    ("ECE", "Block B", 3100000.0),
    ("MECH", "Block C", 2800000.0),
    ("CIVIL", "Block C", 2200000.0),
    ("PHY", "Annex", 1400000.0),
    ("MATH", "Annex", 1250000.0),
    ("CHEM", "Main Hall", 1180000.0),
    ("BIO", "Main Hall", 990000.0),
]
COURSE_WORDS = ["Introduction to", "Advanced", "Applied", "Foundations of",
                "Principles of", "Topics in"]
COURSE_SUBJECTS = ["Algorithms", "Databases", "Networks", "Thermodynamics",
                   "Mechanics", "Signals", "Optics", "Calculus", "Statistics",
                   "Structures", "Materials", "Robotics", "Genetics",
                   "Organic Chemistry", "Machine Learning"]
GRADES = ["A", "B", "C", "D", "F"]
SEMESTERS = ["2023-Fall", "2024-Spring", "2024-Fall", "2025-Spring"]
CATEGORIES = ["Electronics", "Furniture", "Stationery", "Apparel", "Kitchen", "Sports"]
STATUSES = ["placed", "shipped", "delivered", "cancelled"]
PRODUCT_WORDS = ["Desk", "Lamp", "Chair", "Monitor", "Keyboard", "Mouse", "Bottle",
                 "Notebook", "Backpack", "Headset", "Mat", "Kettle", "Jacket",
                 "Racket", "Pan", "Shelf", "Cable", "Speaker", "Tripod", "Mug"]
ADJECTIVES = ["Compact", "Deluxe", "Classic", "Pro", "Lite", "Ultra", "Eco", "Max"]


def fresh(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def build_university(path: Path) -> None:
    rng = random.Random(SEED)
    conn = fresh(path)
    conn.executescript(
        """
        CREATE TABLE department (
            id      INTEGER PRIMARY KEY,
            name    TEXT    NOT NULL UNIQUE,
            building TEXT,
            budget  REAL
        );
        CREATE TABLE faculty (
            id          INTEGER PRIMARY KEY,
            name        TEXT    NOT NULL,
            dept_id     INTEGER NOT NULL REFERENCES department(id),
            designation TEXT,
            salary      REAL
        );
        CREATE TABLE student (
            id      INTEGER PRIMARY KEY,
            name    TEXT    NOT NULL,
            dept_id INTEGER NOT NULL REFERENCES department(id),
            gpa     REAL,
            year    INTEGER,
            city    TEXT
        );
        CREATE TABLE course (
            id         INTEGER PRIMARY KEY,
            title      TEXT    NOT NULL,
            dept_id    INTEGER NOT NULL REFERENCES department(id),
            faculty_id INTEGER          REFERENCES faculty(id),
            credits    INTEGER
        );
        CREATE TABLE enrollment (
            student_id INTEGER NOT NULL REFERENCES student(id),
            course_id  INTEGER NOT NULL REFERENCES course(id),
            semester   TEXT    NOT NULL,
            grade      TEXT,
            PRIMARY KEY (student_id, course_id, semester)
        );
        """
    )
    conn.executemany(
        "INSERT INTO department (id, name, building, budget) VALUES (?,?,?,?)",
        [(i + 1, n, b, bud) for i, (n, b, bud) in enumerate(DEPARTMENTS)],
    )
    dept_ids = list(range(1, len(DEPARTMENTS) + 1))

    faculty = []
    for fid in range(1, 61):
        faculty.append((
            fid,
            f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            rng.choice(dept_ids),
            rng.choice(DESIGNATIONS),
            round(rng.uniform(600000, 2400000), 2),
        ))
    conn.executemany(
        "INSERT INTO faculty (id, name, dept_id, designation, salary) VALUES (?,?,?,?,?)",
        faculty,
    )

    students = []
    for sid in range(1, 2001):
        students.append((
            sid,
            f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            rng.choice(dept_ids),
            round(rng.uniform(5.0, 10.0), 2),
            rng.randint(1, 4),
            rng.choice(CITIES),
        ))
    # Two students deliberately share a name, so the DISTINCT nuance in the
    # semi-join rule is visible in the data.
    students[11] = (12, students[10][1], students[11][2], students[11][3],
                    students[11][4], students[11][5])
    conn.executemany(
        "INSERT INTO student (id, name, dept_id, gpa, year, city) VALUES (?,?,?,?,?,?)",
        students,
    )

    # Two departments are left with no courses, so semi-join answers differ
    # from a plain listing.
    course_depts = dept_ids[:-2]
    courses = []
    for cid in range(1, 121):
        title = f"{rng.choice(COURSE_WORDS)} {rng.choice(COURSE_SUBJECTS)} {cid}"
        fac = rng.choice([f[0] for f in faculty]) if rng.random() > 0.15 else None
        courses.append((cid, title, rng.choice(course_depts), fac, rng.choice([2, 3, 4, 5])))
    conn.executemany(
        "INSERT INTO course (id, title, dept_id, faculty_id, credits) VALUES (?,?,?,?,?)",
        courses,
    )

    seen = set()
    enrollments = []
    while len(enrollments) < 15000:
        key = (rng.randint(1, 2000), rng.randint(1, 120), rng.choice(SEMESTERS))
        if key in seen:
            continue
        seen.add(key)
        enrollments.append((*key, rng.choice(GRADES)))
    conn.executemany(
        "INSERT INTO enrollment (student_id, course_id, semester, grade) VALUES (?,?,?,?)",
        enrollments,
    )
    conn.commit()
    conn.close()


def _retail_rows() -> dict[str, list[tuple]]:
    rng = random.Random(SEED + 1)
    customers = []
    for cid in range(1, 501):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        customers.append((
            cid,
            name,
            f"user{cid}@example.com",
            rng.choice(CITIES),
            rng.choice(COUNTRIES),
            f"202{rng.randint(2,5)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
        ))

    products = []
    for pid in range(1, 201):
        products.append((
            pid,
            f"{rng.choice(ADJECTIVES)} {rng.choice(PRODUCT_WORDS)} {pid}",
            rng.choice(CATEGORIES),
            round(rng.uniform(50, 9000), 2),
            rng.randint(0, 400),
        ))

    # Some customers deliberately have no orders at all.
    ordering = list(range(1, 421))
    orders = []
    for oid in range(1, 1501):
        orders.append((
            oid,
            rng.choice(ordering),
            f"202{rng.randint(3,5)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
            rng.choice(STATUSES),
            0.0,
        ))

    items = []
    totals: dict[int, float] = {}
    for oid in range(1, 1501):
        for pid in rng.sample(range(1, 201), rng.randint(1, 4)):
            qty = rng.randint(1, 6)
            price = products[pid - 1][3]
            items.append((oid, pid, qty, price))
            totals[oid] = round(totals.get(oid, 0.0) + qty * price, 2)
    orders = [(o[0], o[1], o[2], o[3], totals.get(o[0], 0.0)) for o in orders]
    return {"customers": customers, "products": products,
            "orders": orders, "order_items": items}


RETAIL_DDL = """
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    name        TEXT,
    email       TEXT UNIQUE,
    city        TEXT,
    country     TEXT,
    signup_date TEXT
);
CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    name       TEXT,
    category   TEXT,
    price      REAL    NOT NULL,
    stock      INTEGER
);
CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date  TEXT,
    status      TEXT,
    total       REAL
);
CREATE TABLE order_items (
    order_id   INTEGER NOT NULL REFERENCES orders(order_id),
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    quantity   INTEGER,
    unit_price REAL,
    PRIMARY KEY (order_id, product_id)
);
"""


def build_retail(path: Path) -> None:
    conn = fresh(path)
    conn.executescript(RETAIL_DDL)
    rows = _retail_rows()
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", rows["customers"])
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?)", rows["products"])
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?)", rows["orders"])
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?)", rows["order_items"])
    conn.commit()
    conn.close()


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def build_retail_dump(path: Path) -> None:
    """A mysqldump-style rendering of the same retail data."""
    rows = _retail_rows()
    ddl = {
        "customers": """CREATE TABLE `customers` (
  `customer_id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(120) DEFAULT NULL,
  `email` varchar(160) DEFAULT NULL,
  `city` varchar(80) DEFAULT NULL,
  `country` varchar(80) DEFAULT NULL,
  `signup_date` date DEFAULT NULL,
  PRIMARY KEY (`customer_id`),
  UNIQUE KEY `email` (`email`)
) ENGINE=InnoDB AUTO_INCREMENT=501 DEFAULT CHARSET=utf8mb4;""",
        "products": """CREATE TABLE `products` (
  `product_id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(160) DEFAULT NULL,
  `category` varchar(80) DEFAULT NULL,
  `price` double NOT NULL,
  `stock` int(11) DEFAULT NULL,
  PRIMARY KEY (`product_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        "orders": """CREATE TABLE `orders` (
  `order_id` int(11) NOT NULL AUTO_INCREMENT,
  `customer_id` int(11) NOT NULL,
  `order_date` date DEFAULT NULL,
  `status` varchar(40) DEFAULT NULL,
  `total` double DEFAULT NULL,
  PRIMARY KEY (`order_id`),
  KEY `idx_orders_customer` (`customer_id`),
  CONSTRAINT `fk_orders_customer` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`customer_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
        "order_items": """CREATE TABLE `order_items` (
  `order_id` int(11) NOT NULL,
  `product_id` int(11) NOT NULL,
  `quantity` int(11) DEFAULT NULL,
  `unit_price` double DEFAULT NULL,
  PRIMARY KEY (`order_id`,`product_id`),
  KEY `idx_items_product` (`product_id`),
  CONSTRAINT `fk_items_order` FOREIGN KEY (`order_id`) REFERENCES `orders` (`order_id`),
  CONSTRAINT `fk_items_product` FOREIGN KEY (`product_id`) REFERENCES `products` (`product_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;""",
    }
    out: list[str] = [
        "-- MySQL dump 10.13  Distrib 8.0.36",
        "-- Host: localhost    Database: retail",
        "/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;",
        "/*!40101 SET NAMES utf8mb4 */;",
        "SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS;",
        "",
    ]
    for table in ("customers", "products", "orders", "order_items"):
        out.append(f"DROP TABLE IF EXISTS `{table}`;")
        out.append(ddl[table])
        out.append("")
        out.append(f"LOCK TABLES `{table}` WRITE;")
        data = rows[table]
        for start in range(0, len(data), 200):
            chunk = data[start : start + 200]
            values = ",".join(
                "(" + ",".join(_sql_literal(v) for v in row) + ")" for row in chunk
            )
            out.append(f"INSERT INTO `{table}` VALUES {values};")
        out.append("UNLOCK TABLES;")
        out.append("")
    out.append("/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


def build_nokeys(path: Path) -> None:
    """A schema with key-shaped names but no declared constraints at all."""
    rng = random.Random(SEED + 2)
    conn = fresh(path)
    conn.executescript(
        """
        CREATE TABLE team (
            id   INTEGER,
            name TEXT,
            city TEXT
        );
        CREATE TABLE player (
            id      INTEGER,
            name    TEXT,
            team_id INTEGER,
            score   INTEGER
        );
        CREATE TABLE match_result (
            id       INTEGER,
            team_id  INTEGER,
            opponent TEXT,
            points   INTEGER
        );
        """
    )
    teams = [(i, f"Team {chr(64 + i)}", rng.choice(CITIES)) for i in range(1, 7)]
    conn.executemany("INSERT INTO team VALUES (?,?,?)", teams)
    players = [
        (i, f"{rng.choice(FIRST)} {rng.choice(LAST)}", rng.randint(1, 6), rng.randint(0, 99))
        for i in range(1, 61)
    ]
    # A deliberate duplicate row: with no PK, nothing prevents it.
    players.append(players[0])
    conn.executemany("INSERT INTO player VALUES (?,?,?,?)", players)
    results = [
        (i, rng.randint(1, 6), f"Team {chr(64 + rng.randint(1, 6))}", rng.randint(0, 5))
        for i in range(1, 41)
    ]
    conn.executemany("INSERT INTO match_result VALUES (?,?,?,?)", results)
    conn.commit()
    conn.close()


BUILDERS = {
    "university": lambda: build_university(SAMPLES / "university.db"),
    "retail": lambda: build_retail(SAMPLES / "retail.db"),
    "dump": lambda: build_retail_dump(SAMPLES / "retail_dump.sql"),
    "nokeys": lambda: build_nokeys(SAMPLES / "nokeys.db"),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=sorted(BUILDERS), action="append")
    args = ap.parse_args()
    names = args.only or list(BUILDERS)
    for name in names:
        BUILDERS[name]()
        print(f"built {name}")


if __name__ == "__main__":
    main()
