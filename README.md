# Miku Cyber Screen

MatrixPlanet USB serial display dashboard project.

## Requirements

Install dependencies with `python -m pip install -r requirements.txt`.

## Run

Connect the compatible display and run `python main.py`. The application currently uses COM5; change `COM_PORT` in `main.py` for your system. Configuration is created locally under `config/layout_config.json` by the editor.

## Configuration

Run `python gui_editor.py` to adjust the layout and city. Local configuration and runtime logs are excluded from version control.
