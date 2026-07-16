import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

ticker = 'RELIANCE.NS'
df = yf.download(ticker, period='1d', interval='1m', progress=False)

if df.empty:
    print("No data returned")
    exit()

# flatten MultiIndex columns from yfinance
if isinstance(df.columns, __import__('pandas').MultiIndex):
    df.columns = df.columns.get_level_values(0)

print(f"Downloaded {len(df)} candles for {ticker}")
print(df[['Open','High','Low','Close','Volume']].tail(5).to_string())

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 7), gridspec_kw={'height_ratios': [3, 1]})
fig.patch.set_facecolor('#0d0f14')

# price
ax1.set_facecolor('#111318')
ax1.plot(df.index, df['Close'], color='#60a5fa', lw=1.2)
ax1.fill_between(df.index, df['Low'], df['High'], alpha=0.15, color='#60a5fa')
ax1.set_ylabel('Price (₹)', color='#9ca3af')
ax1.tick_params(colors='#9ca3af')
ax1.set_title(f'{ticker} — Today 1-min', color='#fbbf24', fontsize=12)
for sp in ax1.spines.values(): sp.set_edgecolor('#1e2027')
ax1.grid(color='#1e2027', lw=0.5)
ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

# volume
ax2.set_facecolor('#111318')
ax2.bar(df.index, df['Volume'], color='#374151', width=0.0005)
ax2.set_ylabel('Volume', color='#9ca3af')
ax2.tick_params(colors='#9ca3af')
for sp in ax2.spines.values(): sp.set_edgecolor('#1e2027')
ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

plt.tight_layout()
plt.savefig('plot_live.png', dpi=150, facecolor='#0d0f14', bbox_inches='tight')
print('Saved: plot_live.png')
