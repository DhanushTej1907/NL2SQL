# Demo questions

Questions the demo runs, grouped by database. `scripts/demo_check.py` reads
this file: a `##` heading names the database, a `###` heading names the
expected source, and each `- ` line is one question. The
`<!-- expect: ... -->` comment lists the rewrite rules that must fire.

A **compiler** question must compile without the language model, execute,
and pass the equivalence check between its naive and optimized SQL. An
**llm** question passes if the fallback answered and validated it, or if the
fallback was unavailable and said so cleanly.

## university.db

### compiler

- show students with gpa above 9.5
- show the name and gpa of students with gpa above 9.8
- list names of students in CSE
- how many students are there
- what is the highest gpa
- average gpa per department  <!-- expect: R3 -->
- how many courses per department  <!-- expect: R3 -->
- top 5 students by gpa
- show students sorted by gpa descending
- is there any student with gpa above 9.9  <!-- expect: R1 -->
- are there any courses with credits above 4  <!-- expect: R1 -->
- which departments have students with gpa over 9.9  <!-- expect: R2, R3 -->
- which departments have faculty with salary more than 2300000  <!-- expect: R2, R3 -->
- show the name and salary of faculty with salary more than 2000000
- list names of students per department  <!-- expect: R4 -->

### llm

Complex queries, outside the controlled grammar.

- for every department show the number of students whose gpa is above the overall average gpa
- rank the departments by the share of students scoring above 9, highest share first

## retail.db

### compiler
    
- show products with price above 8000
- show the name and price of products in Electronics
- how many orders are there
- how many order items are there
- what is the average price of products
- list distinct cities of customers
- how many orders per customer  <!-- expect: R3 -->
- top 3 products by price
- show orders sorted by total descending
- is there any product with price above 8900  <!-- expect: R1 -->
- are there any orders with total above 90000  <!-- expect: R1 -->
- which customers have orders with total above 90000  <!-- expect: R2, R3 -->
- which products have order items with quantity above 5  <!-- expect: R2, R3 -->
- show the name and stock of products with stock below 10

### llm

- which customers spent more in 2025 than they did in 2024
- for each category show the product whose price is closest to the average price

## nokeys.db

A schema with no declared primary or foreign keys. These show the rules
safely *not* firing: R1 needs no constraints and still fires, while R2 and
R4 appear in the skipped list because every edge here is inferred.

### compiler

- how many players are there
- show players with score above 80
- is there any player with score above 95  <!-- expect: R1 -->
- which teams have players with score above 90  <!-- expect: R3 -->

## retail_dump.sql

The same retail data loaded from a MySQL-style dump, to show the dump path
behaves identically.

### compiler

- show the name and price of products in Electronics
- how many orders are there
- which customers have orders with total above 90000  <!-- expect: R2, R3 -->
- is there any product with price above 8900  <!-- expect: R1 -->
