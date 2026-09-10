# tessdata

`ssd.traineddata` ist ein Tesseract-Modell für Sieben-Segment-Anzeigen
(https://github.com/Shreeshrii/tessdata_ssd). Das mitgelieferte `eng`-Modell
liest die Balkenziffern der Waage deutlich schlechter, besonders die `0`, die
es regelmäßig für eine `9` hält.

Die Datei liegt bewusst im Repo: so braucht der Server keinen Netzzugriff und
die Erkennung verhält sich überall gleich. Fehlt sie, fällt die App
automatisch auf `eng` zurück.
