<h1 align="center">
  <br>
  <b>Project Fundamentals of Data Science:</b>
  <br>
  <i>Market Behavior for the Shared Bicycle Systems in Mexico City</i>
  <br>
</h1>

<p align="center">
  <b>By:</b> Adrian Galicia Agonizante, Miguel Sepúlveda Furber, Luis Enrique Noguera Gil
  <br>
</p>

<p align="center">
  <b>Master's in Data Science</b>
  <br>
  <i>Panamerican University</i>
</p>


## Technologies Used

- Python 3 ![Python](https://img.shields.io/badge/Python-3.11-blue)

## Project Abstract

This study investigates Ecobici's success in Mexico City and how it has outperformed its private sector competitors. It analyzes key factors such as costs, business models and expansion strategies to understand its competitive advantage. It draws on market data, rider behavior and safety to project its future growth.

*Key words:* Ecobici, bike sharing, urban mobility, competitors, expansion, Mexico City.

## Project Objective

Based on the growing trend in Mexico City in recent years for the use of Bici as transportation use and the implementation of infrastructure such as bike lanes and initiatives such as Sunday rides. Among the major providers of this transportation is the public initiative ``ecobici'' which is located in different parts of the city by subscription. One of the intermittents that occurs in this turn is the lack of private sector, since old competitors such as MOBIKE and JUMP that offered bikes with patented parts and an alternative business model that was charged by old, were forced to leave the local and global market. However, Ecobici continues to grow with an efficient maintenance and distribution system. What we are looking for is to find out part of how they keep their operation going and how they have combated the challenges of the industry and maintain their growth. 

## Article

In this repository, you'll find the full article in PDF format located in the `reports` directory. The article contains detailed information and comprehensive analysis on the market behavior of shared bicycle systems in Mexico City. It covers all the methodologies, data explorations, and insights gathered throughout the project.


## Prerequisites

- [Anaconda](https://www.anaconda.com/download/) >=4.x
- Optional [Mamba](https://mamba.readthedocs.io/en/latest/)

## Create environment

```bash
conda env create -f environment.yml
activate ecobici
```

or 

```bash
conda install -c conda-forge mamba
mamba env create -f environment.yml
activate ecobici
```

## Modules and default modules

To install the default modules located in the `scripts` directory, use the following command:

```bash
pip install --editable .
```

For more information about the user's modules, refer to `install.md`.

## The resulting directory structure

The directory structure of your new project will look something like this (depending on the settings that you choose):

```
├── LICENSE            <- Open-source license if one is chosen
├── README.md          <- The top-level README for developers using this project.
├── module         <- User module directory
├── data
│   ├── external       <- Data from third party sources.
│   ├── interim        <- Intermediate data that has been transformed.
│   ├── processed      <- The final, canonical data sets for modeling.
│   └── raw            <- The original, immutable data dump.
│
├── docs               <- A default mkdocs project; see www.mkdocs.org for details
│
├── models             <- Trained and serialized models, model predictions, or model summaries
│
├── notebooks          <- Jupyter notebooks. Naming convention is a number (for ordering),
│                         the creator's initials, and a short `-` delimited description, e.g.
│                         `1.0-anony-initial-data-exploration`.
│
├── scripts            <- Default modules and scripts for the project
│
├── references         <- Data dictionaries, manuals, and all other explanatory materials.
│
├── reports            <- Generated analysis as HTML, PDF, LaTeX, etc.
│   └── figures        <- Generated graphics and figures to be used in reporting
│   └── <>.pdf         <- Article and report of the project.
│
├── requirements.txt   <- The requirements file for reproducing the analysis environment, e.g.
│                         generated with `pip freeze > requirements.txt`
```