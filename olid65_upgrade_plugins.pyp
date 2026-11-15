import json
import os
import re
import shutil
import socket
import ssl
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile

import c4d

PLUGIN_ID = 1059297
CATALOG = "https://raw.githubusercontent.com/olid65/update_plugins_c4d_olid65/main/__lst_url__.json"
status = c4d.gui if hasattr(c4d.gui, "StatusSetBar") else c4d


def explain(error):
    reason = getattr(error, "reason", error)
    if isinstance(error, HTTPError):
        hint = {403: "Accès refusé (droits ou filtrage réseau).",
                404: "Dépôt ou branche introuvable.",
                407: "Le proxy exige une authentification."}.get(error.code, "")
        return f"{error} — {error.geturl()} {hint}".strip()
    if isinstance(reason, ssl.SSLError):
        return f"Connexion TLS refusée. Vérifier le certificat et la confiance du poste. {reason}"
    if isinstance(reason, socket.gaierror):
        return f"Résolution DNS impossible. {reason}"
    if isinstance(reason, TimeoutError):
        return f"Délai réseau dépassé. {reason}"
    if isinstance(error, BadZipFile):
        return f"Archive ZIP invalide ou incomplète. {error}"
    if isinstance(error, PermissionError):
        return f"Accès au disque refusé. Vérifier les droits et les fichiers ouverts. {error}"
    return str(error) or type(error).__name__


def catalog():
    try:
        with urlopen(CATALOG, timeout=30) as response:
            data = response.read()
        warning = ""
    except (URLError, OSError) as error:
        warning = f"Catalogue distant inaccessible\n{CATALOG}\n{explain(error)}"
        try:
            data = Path(__file__).with_name("__lst_url__.json").read_bytes()
        except OSError as local_error:
            raise ValueError(f"{warning}\nCopie locale inaccessible : {local_error}") from local_error
        warning += "\nUtilisation du catalogue local.\n\n"
    urls = json.loads(data)
    if not isinstance(urls, list) or not urls:
        raise ValueError("Le catalogue doit contenir une liste non vide de liens GitHub.")
    items = []
    names = set()
    for url in urls:
        parts = urlsplit(url) if isinstance(url, str) else None
        match = re.fullmatch(r"/[\w.-]+/([\w.-]+)/archive/refs/heads/([\w.-]+)\.zip", parts.path) if parts else None
        if not match or parts.scheme != "https" or parts.netloc != "github.com":
            raise ValueError(f"Lien de catalogue non reconnu : {url}")
        name, branch = match.groups()
        if name.endswith(".") or PureWindowsPath(name).is_reserved() or name.casefold() in names:
            raise ValueError(f"Nom de dossier invalide ou en double : {name}")
        names.add(name.casefold())
        items.append((name, branch, url))
    return items, warning


def destination(parent, name, branch):
    paths = [parent / n for n in dict.fromkeys((name, f"{name}-{branch}", f"{name}-main", f"{name}-master"))]
    existing = [p for p in paths if os.path.lexists(p)]
    if len(existing) > 1:
        raise ValueError("Plusieurs installations à départager : " + ", ".join(map(str, existing)))
    path = existing[0] if existing else paths[0]
    if existing and (not path.is_dir() or path.is_symlink() or path.resolve() != path.absolute()):
        raise ValueError(f"Dossier de destination inhabituel : {path}")
    return path


def install(parent, target, name, branch, url, progress):
    # Outside the scanned plugins directory, on the same filesystem for renames.
    with tempfile.TemporaryDirectory(prefix="olid65-", dir=parent.parent) as tmp:
        work = Path(tmp)
        archive = work / "plugin.zip"
        with urlopen(url, timeout=45) as response, archive.open("wb") as output:
            size = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if size:
                    progress("Téléchargement", min(received / size, 1) * 0.8)
            if size and received != size:
                raise ValueError(f"Téléchargement incomplet ({received} / {size} octets)")
        root = f"{name}-{branch}"
        progress("Extraction", 0.8)
        with ZipFile(archive) as package:
            for entry in package.infolist():
                parts = entry.filename.rstrip("/").split("/")
                if parts[0] != root or any(
                    not p or p.endswith((".", " ")) or "\\" in p or ":" in p
                    or PureWindowsPath(p).is_reserved() for p in parts
                ):
                    raise ValueError(f"Chemin inattendu dans le ZIP : {entry.filename}")
            package.extractall(work)
        staged = work / root
        if not staged.is_dir() or not any(staged.iterdir()):
            raise ValueError("L’archive ne contient aucun fichier du plugin.")
        progress("Installation", 0.95)
        # Keep the recovery copy outside temporary cleanup until replacement succeeds.
        if target.exists():
            backup = Path(tempfile.mkdtemp(prefix="olid65-sauvegarde-", dir=parent.parent))
            previous = backup / target.name
            try:
                target.rename(previous)
            except OSError:
                backup.rmdir()
                raise
            try:
                staged.rename(target)
            except OSError:
                try:
                    previous.rename(target)
                except OSError as restore_error:
                    raise RuntimeError(f"Restauration manuelle nécessaire depuis {previous} : {restore_error}")
                backup.rmdir()
                raise
            try:
                shutil.rmtree(backup)
            except OSError as error:
                return f"Ancienne copie conservée dans {backup} ({error})"
        else:
            staged.rename(target)
    return ""


class UpgradePluginsOlid65(c4d.plugins.CommandData):
    def Execute(self, doc):
        parent = Path(c4d.storage.GeGetStartupWritePath()) / "plugins"
        installed, new, plan, errors, done = [], [], [], [], []
        try:
            status.StatusSetText("Lecture du catalogue de plugins…")
            status.StatusSetSpin()
            items, warning = catalog()
            for name, branch, url in items:
                try:
                    path = destination(parent, name, branch)
                except ValueError as error:
                    errors.append(f"{name} — {error}")
                    continue
                (installed if path.exists() else new).append(path.name)
                plan.append((path, name, branch, url))
            sections = [f"Dossier des plugins\n{parent}"]
            if installed:
                sections.append("Déjà installés — dossiers à remplacer\n" + "\n".join(installed))
            if new:
                sections.append("À installer\n" + "\n".join(new))
            if errors:
                sections.append("Ignorés\n" + "\n".join(errors))
            status.StatusClear()
            if not plan:
                c4d.gui.MessageDialog(warning + "\n\n".join(sections))
                return True
            if not c4d.gui.QuestionDialog(warning + "\n\n".join(sections) + "\n\nContinuer ?"):
                return True
            for index, (path, name, branch, url) in enumerate(plan):
                phase = "Téléchargement"

                def progress(label, fraction):
                    nonlocal phase
                    phase = label
                    status.StatusSetText(f"{index + 1}/{len(plan)} — {name} — {label}")
                    status.StatusSetBar(int(100 * (index + fraction) / len(plan)))

                try:
                    progress(phase, 0)
                    note = install(parent, path, name, branch, url, progress)
                    done.append(name + (f" — {note}" if note else ""))
                except Exception as error:
                    errors.append(f"{name} — {phase}\n{url}\n{explain(error)}")
                status.StatusSetBar(int(100 * (index + 1) / len(plan)))
        except Exception as error:
            c4d.gui.MessageDialog(f"Préparation impossible\n{CATALOG}\n{explain(error)}")
            return False
        finally:
            status.StatusClear()

        report = []
        if done:
            report.append("Installation terminée\n" + "\n".join(done))
        if errors:
            report.append("Échecs ou plugins ignorés\n" + "\n\n".join(errors))
        message = "\n\n".join(report)
        print(message)
        if done:
            if c4d.gui.QuestionDialog(message + "\n\nRedémarrer Cinema 4D pour charger les plugins ?"):
                c4d.RestartMe()
        else:
            c4d.gui.MessageDialog(message)
        return True


if __name__ == "__main__":
    c4d.plugins.RegisterCommandPlugin(
        id=PLUGIN_ID, str="Mettre à jour les plugins d'olid65", info=0,
        help="Télécharge et installe les plugins C4D d'olid65 depuis GitHub",
        dat=UpgradePluginsOlid65(), icon=None)
