"""Render an expense chart in memory, without a GUI or pyplot state."""
from io import BytesIO
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.ticker import FuncFormatter
from db.request import format_money


def render_chart(data):
    figure = Figure(figsize=(11, 5), dpi=120, layout='constrained')
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    positions = list(range(len(data['labels'])))
    axes.bar(positions, data['amounts'], color='#4f73df')
    axes.set_xticks(positions, data['labels'], rotation=60 if len(positions) > 12 else 0)
    axes.set_title(data['title'])
    axes.set_ylabel(f"Расходы, {format_money(0, data['currency']).split()[-1]}")
    axes.yaxis.set_major_formatter(FuncFormatter(lambda amount, position: format_money(round(amount), data['currency'])))
    axes.grid(axis='y', alpha=0.25)
    axes.set_axisbelow(True)
    output = BytesIO()
    figure.savefig(output, format='png')
    return output.getvalue()
