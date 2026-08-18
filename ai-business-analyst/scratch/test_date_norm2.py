import pandas as pd
from sqlalchemy import create_engine, inspect, DATE, DATETIME, Text

engine = create_engine("sqlite:///:memory:")

dates_only = pd.to_datetime(["5/28/2010", "6/15/2011", "invalid"], errors="coerce", dayfirst=False)
datetimes = pd.to_datetime(["5/28/2010 14:30:00", "6/15/2011 09:15:22", "invalid"], errors="coerce", dayfirst=False)

df = pd.DataFrame({
    "order_date": dates_only.date, # array of python datetime.date and None/NaT
    "order_datetime": datetimes,
})

# Let's test with dtype
df.to_sql("test_tbl", con=engine, index=False, dtype={"order_date": DATE(), "order_datetime": DATETIME()})

with engine.connect() as conn:
    rows = conn.exec_driver_sql("SELECT order_date, order_datetime, strftime('%Y', order_date), strftime('%H', order_datetime) FROM test_tbl").all()
    print("Rows:", rows)

inspector = inspect(engine)
for c in inspector.get_columns("test_tbl"):
    print(c["name"], c["type"])
