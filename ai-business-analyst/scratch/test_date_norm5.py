import pandas as pd
from sqlalchemy import create_engine, inspect, DATE, DATETIME, Text

engine = create_engine("sqlite:///:memory:")

# Test chunk 1:
raw_dates = ["5/28/2010", "6/15/2011", "not_a_date", None]
parsed = pd.to_datetime(raw_dates, errors="coerce", dayfirst=False)
date_objs = [d.date() if pd.notna(d) else None for d in parsed]

df = pd.DataFrame({"order_date": date_objs})
df.to_sql("orders", con=engine, index=False, dtype={"order_date": DATE()})

with engine.connect() as conn:
    print("Stored rows:", conn.exec_driver_sql("SELECT order_date, strftime('%Y', order_date), strftime('%m', order_date), strftime('%d', order_date) FROM orders").all())

inspector = inspect(engine)
print("Introspected type:", inspector.get_columns("orders")[0]["type"])
